"""Phase 3 + 6: ONVIF device / media / media2 / events / imaging services."""
from __future__ import annotations

import asyncio
import random
import time
from datetime import timedelta
from xml.etree import ElementTree as ET

from . import auth, soap
from .soap import envelope, esc, fault, iso, text_of

MOTION_TOPIC = "tns1:RuleEngine/CellMotionDetector/Motion"
TAMPER_TOPIC = "tns1:RuleEngine/TamperDetector/Tamper"
SIGNAL_TOPIC = "tns1:VideoSource/SignalLoss"

NO_AUTH = {"GetSystemDateAndTime", "GetCapabilities", "GetServices",
           "GetServiceCapabilities", "GetWsdlUrl"}


# --------------------------------------------------------------- events
class EventManager:
    MAX_SUBS = 64            # per camera; a VMS that never unsubscribes
    MAX_QUEUE = 500          # messages kept per subscription

    def __init__(self, cam):
        self.cam = cam
        self.subs: dict[str, dict] = {}
        self._motion_task = None

    def sweep(self):
        """Drop subscriptions past their TerminationTime, as a real device
        does. Without this every reconnecting VMS left one behind for ever,
        each holding up to 500 queued messages."""
        now = time.time()
        for sid in [k for k, s in self.subs.items() if now > s["expires"]]:
            s = self.subs.pop(sid, None)
            if s is not None:
                s["evt"].set()         # a PullMessages waiting on it returns now
            self.cam.log(f"event subscription {sid} expired")

    def create(self, timeout: int) -> str:
        self.sweep()
        while len(self.subs) >= self.MAX_SUBS:
            oldest = min(self.subs, key=lambda k: self.subs[k]["created"])
            self.subs.pop(oldest, None)
            self.cam.log(f"event subscription {oldest} evicted (limit)")
        sid = f"sub{random.getrandbits(28):07x}"
        now = time.time()
        self.subs[sid] = {"msgs": [], "expires": now + timeout, "created": now,
                          "evt": asyncio.Event()}
        self.cam.log(f"event subscription {sid} (timeout {timeout}s)")
        return sid

    def renew(self, sid: str, timeout: int) -> bool:
        self.sweep()
        s = self.subs.get(sid)
        if not s:
            return False
        s["expires"] = time.time() + timeout
        return True

    def expires_at(self, sid: str) -> float:
        s = self.subs.get(sid)
        return s["expires"] if s else time.time()

    def resolve(self, sid: str) -> str:
        """The subscription a request is aimed at.

        The id normally rides in the query string of the address we handed
        out; a client that posts to the bare event service URL with a single
        live subscription clearly means that one."""
        if sid in self.subs:
            return sid
        if not sid and len(self.subs) == 1:
            return next(iter(self.subs))
        return sid

    def drop(self, sid: str):
        s = self.subs.pop(sid, None)
        if s is not None:
            s["evt"].set()

    def _msg(self, topic: str, items: list[tuple[str, str]],
             source_name: str = "VideoSourceConfigurationToken",
             source_val: str = "VideoSourceConfig") -> str:
        now = iso(self.cam.now())
        data = "".join(f'<tt:SimpleItem Name="{k}" Value="{v}"/>' for k, v in items)
        return (
            '<wsnt:NotificationMessage>'
            '<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
            f'{topic}</wsnt:Topic><wsnt:Message>'
            f'<tt:Message UtcTime="{now}" PropertyOperation="Changed">'
            f'<tt:Source><tt:SimpleItem Name="{source_name}" Value="{source_val}"/>'
            '<tt:SimpleItem Name="RuleName" Value="MyMotionDetectorRule"/></tt:Source>'
            f'<tt:Data>{data}</tt:Data></tt:Message>'
            '</wsnt:Message></wsnt:NotificationMessage>')

    def fire(self, topic: str, items: list[tuple[str, str]], quiet: bool = False):
        self.sweep()
        m = self._msg(topic, items)
        for s in self.subs.values():
            s["msgs"].append(m)
            if len(s["msgs"]) > self.MAX_QUEUE:
                del s["msgs"][:-self.MAX_QUEUE]
            s["evt"].set()
        if not quiet:
            self.cam.log(f"event {topic} {dict(items)}")

    def motion(self, on: bool):
        self.fire(MOTION_TOPIC, [("IsMotion", "true" if on else "false")])

    def tamper(self, on: bool):
        self.fire(TAMPER_TOPIC, [("IsTamper", "true" if on else "false")])

    def signal_loss(self, on: bool):
        self.fire(SIGNAL_TOPIC, [("State", "true" if on else "false")])

    async def pull(self, sid: str, timeout: int, limit: int):
        self.sweep()
        s = self.subs.get(sid)
        if s is None:
            return None
        f = self.cam.cfg.faults
        if f.event_stall:
            await asyncio.sleep(timeout * 3)
            return []
        if f.subscription_expire_sec and \
                time.time() - s["created"] > f.subscription_expire_sec:
            self.subs.pop(sid, None)
            return None
        loop = asyncio.get_event_loop()
        end = loop.time() + max(1, timeout)
        while True:
            if f.event_flood:
                self.fire(MOTION_TOPIC,
                          [("IsMotion", "true" if random.random() > 0.5 else "false")],
                          quiet=True)
            if s["msgs"]:
                out = s["msgs"][:limit]
                del s["msgs"][:len(out)]
                if not s["msgs"]:
                    s["evt"].clear()
                return out
            remaining = end - loop.time()
            if remaining <= 0:
                return []
            s["evt"].clear()
            try:
                # wake on the next fire() rather than polling; the flood fault
                # still needs a short tick to keep generating
                await asyncio.wait_for(s["evt"].wait(),
                                       min(remaining, 0.2 if f.event_flood else 30.0))
            except asyncio.TimeoutError:
                pass
            if sid not in self.subs:            # expired or unsubscribed meanwhile
                return None

    async def auto_motion_loop(self):
        """Phase 6: periodic motion when motion_interval_sec > 0.

        Ticks once a second so a changed interval takes effect promptly and
        expired subscriptions are swept even when no client is pulling."""
        try:
            since = 0
            while True:
                await asyncio.sleep(1)
                self.sweep()
                iv = self.cam.cfg.motion_interval_sec
                if iv <= 0:
                    since = 0
                    continue
                since += 1
                if since >= iv:
                    since = 0
                    self.motion(True)
                    await asyncio.sleep(max(1, self.cam.cfg.motion_duration_sec))
                    self.motion(False)
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------- services
class OnvifService:
    def __init__(self, cam):
        self.cam = cam
        self.events = EventManager(cam)

    # ------------------------------------------------------------ helpers
    @property
    def base(self) -> str:
        return self.cam.base_url

    def _svc_xaddrs(self) -> dict:
        b = self.base
        return {
            "device": f"{b}/onvif/device_service",
            "media": f"{b}/onvif/media_service",
            "media2": f"{b}/onvif/media2_service",
            "events": f"{b}/onvif/event_service",
            "imaging": f"{b}/onvif/imaging_service",
        }

    def _profile(self, token: str):
        for p in self.cam.cfg.profiles:
            if p.token == token:
                return p
        return None

    def _venc(self, p) -> str:
        gop = p.gop or p.fps
        codec_el = (f"<tt:H264><tt:GovLength>{gop}</tt:GovLength>"
                    f"<tt:H264Profile>Main</tt:H264Profile></tt:H264>"
                    if p.codec == "H264" else "")
        return (
            f'<tt:VideoEncoderConfiguration token="venc_{esc(p.token)}">'
            f'<tt:Name>venc_{esc(p.token)}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f'<tt:Encoding>{esc(p.codec)}</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{p.width}</tt:Width>'
            f'<tt:Height>{p.height}</tt:Height></tt:Resolution>'
            f'<tt:Quality>5</tt:Quality>'
            f'<tt:RateControl><tt:FrameRateLimit>{p.fps}</tt:FrameRateLimit>'
            f'<tt:EncodingInterval>1</tt:EncodingInterval>'
            f'<tt:BitrateLimit>{p.bitrate}</tt:BitrateLimit></tt:RateControl>'
            f'{codec_el}'
            f'<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>'
            f'<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>'
            f'<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL>'
            f'<tt:AutoStart>false</tt:AutoStart></tt:Multicast>'
            f'<tt:SessionTimeout>PT60S</tt:SessionTimeout>'
            f'</tt:VideoEncoderConfiguration>')

    def _vsrc_cfg(self) -> str:
        p = self.cam.cfg.profiles[0]
        return (
            '<tt:VideoSourceConfiguration token="VideoSourceConfig">'
            '<tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>2</tt:UseCount>'
            '<tt:SourceToken>VideoSource_1</tt:SourceToken>'
            f'<tt:Bounds x="0" y="0" width="{p.width}" height="{p.height}"/>'
            '</tt:VideoSourceConfiguration>')

    def _profile_xml(self, p) -> str:
        return (f'<trt:Profiles token="{esc(p.token)}" fixed="true">'
                f'<tt:Name>{esc(p.name)}</tt:Name>{self._vsrc_cfg()}{self._venc(p)}'
                f'</trt:Profiles>')

    def _profile2_xml(self, p) -> str:
        gop = p.gop or p.fps
        return (
            f'<tr2:Profiles token="{esc(p.token)}" fixed="true">'
            f'<tr2:Name>{esc(p.name)}</tr2:Name><tr2:Configurations>'
            f'<tr2:VideoSource token="VideoSourceConfig">'
            f'<tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>2</tt:UseCount>'
            f'<tt:SourceToken>VideoSource_1</tt:SourceToken>'
            f'<tt:Bounds x="0" y="0" width="{p.width}" height="{p.height}"/>'
            f'</tr2:VideoSource>'
            f'<tr2:VideoEncoder token="venc_{esc(p.token)}" GovLength="{gop}" Profile="Main">'
            f'<tt:Name>venc_{esc(p.token)}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f'<tt:Encoding>{esc(p.codec)}</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{p.width}</tt:Width>'
            f'<tt:Height>{p.height}</tt:Height></tt:Resolution>'
            f'<tt:RateControl ConstantBitRate="false">'
            f'<tt:FrameRateLimit>{p.fps}</tt:FrameRateLimit>'
            f'<tt:BitrateLimit>{p.bitrate}</tt:BitrateLimit></tt:RateControl>'
            f'<tt:Quality>5</tt:Quality>'
            f'</tr2:VideoEncoder></tr2:Configurations></tr2:Profiles>')

    # ------------------------------------------------------------ dispatch
    async def handle(self, path: str, body: bytes, peer: str,
                     http_auth: str | None = None, nonces=None):
        """-> (status, content_type, body, extra_headers) or None to drop."""
        f = self.cam.cfg.faults
        SOAP = "application/soap+xml"
        if f.offline or f.onvif_offline:
            return None                                   # drop connection
        if f.soap_timeout:
            await asyncio.sleep(120)
            return None
        if f.soap_latency_ms:
            await asyncio.sleep(f.soap_latency_ms / 1000.0)

        try:
            action, body_el, sec = soap.parse(body)
        except ET.ParseError:
            return 400, SOAP, fault("Malformed request"), None

        self.cam.log(f"ONVIF {action} <- {peer}")

        if f.soap_fault:
            return 500, SOAP, fault("Injected fault", "ter:Action"), None
        if f.malformed_xml:
            return 200, SOAP, b"<s:Envelope><s:Body><broken>", None

        if action not in NO_AUTH:
            u, pw = self.cam.cfg.username, self.cam.cfg.password
            if sec:
                # WS-Security UsernameToken supplied: a bad one is a SOAP
                # Fault with HTTP 400, exactly as the ONVIF core spec says.
                ok = (not f.reject_auth) and soap.check_wsse(
                    sec, u, pw, self.cam.now())
                if not ok:
                    self.cam.log(f"ONVIF {action} DENIED (WS-Security) <- {peer}")
                    return 400, SOAP, soap.auth_fault(), None
            else:
                # No WS-Security header: fall back to HTTP Digest like a real
                # camera, and always answer with a challenge so the client can
                # retry.  Answering 401 without WWW-Authenticate makes clients
                # report "authentication failed" and give up.
                ok = (not f.reject_auth) and nonces is not None and auth.check(
                    http_auth, "POST", u, pw, nonces)
                if not ok:
                    self.cam.log(f"ONVIF {action} needs auth, sending challenge "
                                 f"-> {peer}")
                    hdr = {"WWW-Authenticate": auth.challenge(
                        nonces.issue() if nonces else auth.make_nonce())}
                    return 401, SOAP, soap.auth_fault(), hdr

        fn = getattr(self, "op_" + action, None) if action.isidentifier() else None
        if fn is None:
            return 400, SOAP, fault(f"Unsupported: {action}",
                                    "ter:ActionNotSupported"), None
        try:
            res = fn(body_el, path)
            if asyncio.iscoroutine(res):
                res = await res
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # a bug in one operation must answer with a Fault, not drop the
            # TCP connection and look like a dead camera
            self.cam.log(f"ONVIF {action} failed internally: {e!r}")
            return 500, SOAP, fault(f"Internal error in {action}",
                                    "ter:Action", "s:Receiver"), None
        if res is None:
            return 400, SOAP, fault("Bad request"), None
        data = envelope(res) if isinstance(res, str) else res
        status = 200
        if isinstance(res, bytes) and b"<s:Fault>" in res:
            # per ONVIF, Sender faults travel as HTTP 400 and Receiver as 500
            status = 400 if b"<s:Value>s:Sender</s:Value>" in res else 500
        if f.truncate_response:
            data = data[: len(data) // 2]
        return status, SOAP, data, None

    def _profile_or_fault(self, el, tag: str = "ProfileToken"):
        """-> (profile, None) or (None, fault). An empty token means 'the
        main profile'; an unknown one is ter:NoProfile as the spec says."""
        tok = text_of(el, tag)
        if not tok:
            return self.cam.cfg.profiles[0], None
        p = self._profile(tok)
        if p is None:
            return None, fault(f"No profile '{tok}'", "ter:NoProfile", "s:Sender")
        return p, None

    # -------------------------------------------------------------- device
    def op_GetSystemDateAndTime(self, el, path):
        n = self.cam.now()
        dt = (f'<tt:Time><tt:Hour>{n.hour}</tt:Hour>'
              f'<tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>'
              f'<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month>'
              f'<tt:Day>{n.day}</tt:Day></tt:Date>')
        return ('<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>'
                '<tt:DateTimeType>NTP</tt:DateTimeType>'
                '<tt:DaylightSavings>false</tt:DaylightSavings>'
                '<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>'
                f'<tt:UTCDateTime>{dt}</tt:UTCDateTime>'
                f'<tt:LocalDateTime>{dt}</tt:LocalDateTime>'
                '</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>')

    def op_GetNetworkProtocols(self, el, path):
        c = self.cam.cfg
        return ('<tds:GetNetworkProtocolsResponse>'
                f'<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled>'
                f'<tt:Port>{c.onvif_port}</tt:Port></tds:NetworkProtocols>'
                f'<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled>'
                f'<tt:Port>{c.rtsp_port}</tt:Port></tds:NetworkProtocols>'
                '</tds:GetNetworkProtocolsResponse>')

    def op_GetNetworkDefaultGateway(self, el, path):
        return ('<tds:GetNetworkDefaultGatewayResponse><tds:NetworkGateway>'
                '<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tds:NetworkGateway>'
                '</tds:GetNetworkDefaultGatewayResponse>')

    def op_GetNTP(self, el, path):
        return ('<tds:GetNTPResponse><tds:NTPInformation>'
                '<tt:FromDHCP>false</tt:FromDHCP></tds:NTPInformation>'
                '</tds:GetNTPResponse>')

    def op_GetDiscoveryMode(self, el, path):
        mode = "NonDiscoverable" if self.cam.cfg.faults.hide_from_discovery else "Discoverable"
        return (f'<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>{mode}'
                '</tds:DiscoveryMode></tds:GetDiscoveryModeResponse>')

    def op_GetRelayOutputs(self, el, path):
        return '<tds:GetRelayOutputsResponse/>'

    def op_GetZeroConfiguration(self, el, path):
        return ('<tds:GetZeroConfigurationResponse><tds:ZeroConfiguration>'
                '<tt:InterfaceToken>eth0</tt:InterfaceToken>'
                '<tt:Enabled>false</tt:Enabled></tds:ZeroConfiguration>'
                '</tds:GetZeroConfigurationResponse>')

    def op_GetSystemUris(self, el, path):
        return '<tds:GetSystemUrisResponse/>'

    def op_GetWsdlUrl(self, el, path):
        return ('<tds:GetWsdlUrlResponse><tds:WsdlUrl>http://www.onvif.org/'
                '</tds:WsdlUrl></tds:GetWsdlUrlResponse>')

    def op_GetEndpointReference(self, el, path):
        return (f'<tds:GetEndpointReferenceResponse><tds:GUID>{esc(self.cam.cfg.uuid)}'
                '</tds:GUID></tds:GetEndpointReferenceResponse>')

    def op_SetSystemDateAndTime(self, el, path):
        return '<tds:SetSystemDateAndTimeResponse/>'

    def op_GetDeviceInformation(self, el, path):
        c = self.cam.cfg
        return ('<tds:GetDeviceInformationResponse>'
                f'<tds:Manufacturer>{esc(c.manufacturer)}</tds:Manufacturer>'
                f'<tds:Model>{esc(c.model)}</tds:Model>'
                f'<tds:FirmwareVersion>{esc(c.firmware)}</tds:FirmwareVersion>'
                f'<tds:SerialNumber>{esc(c.serial)}</tds:SerialNumber>'
                f'<tds:HardwareId>{esc(c.hardware)}</tds:HardwareId>'
                '</tds:GetDeviceInformationResponse>')

    def op_GetCapabilities(self, el, path):
        x = self._svc_xaddrs()
        return ('<tds:GetCapabilitiesResponse><tds:Capabilities>'
                f'<tt:Device><tt:XAddr>{x["device"]}</tt:XAddr>'
                '<tt:Network><tt:IPFilter>false</tt:IPFilter>'
                '<tt:ZeroConfiguration>false</tt:ZeroConfiguration>'
                '<tt:IPVersion6>false</tt:IPVersion6>'
                '<tt:DynDNS>false</tt:DynDNS></tt:Network>'
                '<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>'
                '<tt:DiscoveryBye>true</tt:DiscoveryBye>'
                '<tt:RemoteDiscovery>false</tt:RemoteDiscovery>'
                '<tt:SystemBackup>false</tt:SystemBackup>'
                '<tt:SystemLogging>false</tt:SystemLogging>'
                '<tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>'
                '<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor>'
                '</tt:SupportedVersions></tt:System>'
                '<tt:Security><tt:TLS1.1>false</tt:TLS1.1><tt:TLS1.2>false</tt:TLS1.2>'
                '<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>'
                '<tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>'
                '<tt:X.509Token>false</tt:X.509Token><tt:SAMLToken>false</tt:SAMLToken>'
                '<tt:KerberosToken>false</tt:KerberosToken>'
                '<tt:RELToken>false</tt:RELToken></tt:Security></tt:Device>'
                f'<tt:Media><tt:XAddr>{x["media"]}</tt:XAddr>'
                '<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>'
                '<tt:RTP_TCP>true</tt:RTP_TCP>'
                '<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities>'
                '</tt:Media>'
                f'<tt:Events><tt:XAddr>{x["events"]}</tt:XAddr>'
                '<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>'
                '<tt:WSPullPointSupport>true</tt:WSPullPointSupport>'
                '<tt:WSPausableSubscriptionManagerInterfaceSupport>false'
                '</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>'
                f'<tt:Imaging><tt:XAddr>{x["imaging"]}</tt:XAddr></tt:Imaging>'
                '</tds:Capabilities></tds:GetCapabilitiesResponse>')

    def op_GetServices(self, el, path):
        x = self._svc_xaddrs()
        def s(ns, addr, minor):
            return (f'<tds:Service><tds:Namespace>{ns}</tds:Namespace>'
                    f'<tds:XAddr>{addr}</tds:XAddr>'
                    f'<tds:Version><tt:Major>2</tt:Major>'
                    f'<tt:Minor>{minor}</tt:Minor></tds:Version></tds:Service>')
        return ('<tds:GetServicesResponse>'
                + s(soap.NS["tds"], x["device"], 60)
                + s(soap.NS["trt"], x["media"], 60)
                + s(soap.NS["tr2"], x["media2"], 60)
                + s(soap.NS["tev"], x["events"], 60)
                + s(soap.NS["timg"], x["imaging"], 60)
                + '</tds:GetServicesResponse>')

    def op_GetServiceCapabilities(self, el, path):
        if "media2" in path:
            return ('<tr2:GetServiceCapabilitiesResponse>'
                    '<tr2:Capabilities SnapshotUri="true" Rotation="false" '
                    'VideoSourceMode="false" OSD="false" '
                    'ProfileCapabilities="true"/>'
                    '</tr2:GetServiceCapabilitiesResponse>')
        if "media" in path:
            return ('<trt:GetServiceCapabilitiesResponse>'
                    '<trt:Capabilities SnapshotUri="true" Rotation="false">'
                    '<trt:StreamingCapabilities RTPMulticast="false" '
                    'RTP_TCP="true" RTP_RTSP_TCP="true"/>'
                    '</trt:Capabilities></trt:GetServiceCapabilitiesResponse>')
        if "event" in path:
            return ('<tev:GetServiceCapabilitiesResponse>'
                    '<tev:Capabilities WSSubscriptionPolicySupport="false" '
                    'WSPullPointSupport="true" MaxNotificationProducers="10" '
                    'MaxPullPoints="10" PersistentNotificationStorage="false"/>'
                    '</tev:GetServiceCapabilitiesResponse>')
        if "imaging" in path:
            return ('<timg:GetServiceCapabilitiesResponse>'
                    '<timg:Capabilities ImageStabilization="false"/>'
                    '</timg:GetServiceCapabilitiesResponse>')
        return ('<tds:GetServiceCapabilitiesResponse><tds:Capabilities>'
                '<tds:Network IPFilter="false" ZeroConfiguration="false" '
                'IPVersion6="false" DynDNS="false" NTP="1"/>'
                '<tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" '
                'Dot1X="false" RemoteUserHandling="false" X.509Token="false" '
                'SAMLToken="false" KerberosToken="false" UsernameToken="true" '
                'HttpDigest="true" RELToken="false"/>'
                '<tds:System DiscoveryResolve="false" DiscoveryBye="true" '
                'RemoteDiscovery="false" SystemBackup="false" SystemLogging="false" '
                'FirmwareUpgrade="false" HttpFirmwareUpgrade="false" '
                'HttpSystemBackup="false" HttpSystemLogging="false" '
                'HttpSupportInformation="false"/>'
                '</tds:Capabilities></tds:GetServiceCapabilitiesResponse>')

    def op_GetScopes(self, el, path):
        c = self.cam.cfg
        scopes = [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/type/Network_Video_Transmitter",
            "onvif://www.onvif.org/Profile/Streaming",
            f"onvif://www.onvif.org/name/{esc(c.name.replace(' ', '_'))}",
            f"onvif://www.onvif.org/hardware/{esc(c.model)}",
            "onvif://www.onvif.org/location/any",
        ]
        return ('<tds:GetScopesResponse>' + "".join(
            f'<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>'
            f'<tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>' for s in scopes)
            + '</tds:GetScopesResponse>')

    def op_GetHostname(self, el, path):
        return ('<tds:GetHostnameResponse><tds:HostnameInformation>'
                '<tt:FromDHCP>false</tt:FromDHCP>'
                f'<tt:Name>{esc(self.cam.cfg.name.replace(" ", "-"))}</tt:Name>'
                '</tds:HostnameInformation></tds:GetHostnameResponse>')

    def op_GetNetworkInterfaces(self, el, path):
        c = self.cam.cfg
        return ('<tds:GetNetworkInterfacesResponse>'
                '<tds:NetworkInterfaces token="eth0">'
                '<tt:Enabled>true</tt:Enabled>'
                f'<tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>{esc(c.mac)}</tt:HwAddress>'
                '<tt:MTU>1500</tt:MTU></tt:Info>'
                '<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>'
                f'<tt:Manual><tt:Address>{esc(c.ip)}</tt:Address>'
                '<tt:PrefixLength>24</tt:PrefixLength></tt:Manual>'
                '<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4>'
                '</tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>')

    def op_GetUsers(self, el, path):
        return ('<tds:GetUsersResponse><tds:User>'
                f'<tt:Username>{esc(self.cam.cfg.username)}</tt:Username>'
                '<tt:UserLevel>Administrator</tt:UserLevel>'
                '</tds:User></tds:GetUsersResponse>')

    def op_GetDNS(self, el, path):
        return ('<tds:GetDNSResponse><tds:DNSInformation>'
                '<tt:FromDHCP>false</tt:FromDHCP></tds:DNSInformation>'
                '</tds:GetDNSResponse>')

    def op_SystemReboot(self, el, path):
        self.cam.log("SystemReboot requested (simulated)")
        return '<tds:SystemRebootResponse><tds:Message>Rebooting</tds:Message>' \
               '</tds:SystemRebootResponse>'

    # --------------------------------------------------------------- media
    def op_GetProfiles(self, el, path):
        if "media2" in path:
            return ('<tr2:GetProfilesResponse>' + "".join(
                self._profile2_xml(p) for p in self.cam.cfg.profiles)
                + '</tr2:GetProfilesResponse>')
        return ('<trt:GetProfilesResponse>' + "".join(
            self._profile_xml(p) for p in self.cam.cfg.profiles)
            + '</trt:GetProfilesResponse>')

    def op_GetProfile(self, el, path):
        p, flt = self._profile_or_fault(el)
        if flt:
            return flt
        return f'<trt:GetProfileResponse>{self._profile_xml(p)}</trt:GetProfileResponse>'

    def op_CreateProfile(self, el, path):
        return fault("Profiles are fixed on this device", "ter:MaxNVTProfiles",
                     "s:Receiver")

    def op_DeleteProfile(self, el, path):
        return fault("Profiles are fixed on this device", "ter:DeletionOfFixedProfile",
                     "s:Sender")

    def op_GetGuaranteedNumberOfVideoEncoderInstances(self, el, path):
        n = len(self.cam.cfg.profiles)
        return ('<trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>'
                f'<trt:TotalNumber>{n}</trt:TotalNumber><trt:H264>{n}</trt:H264>'
                '</trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>')

    def op_GetVideoSourceConfiguration(self, el, path):
        return ('<trt:GetVideoSourceConfigurationResponse>'
                f'{self._vsrc_cfg()}</trt:GetVideoSourceConfigurationResponse>')

    def op_GetVideoSourceConfigurationOptions(self, el, path):
        p = self.cam.cfg.profiles[0]
        return ('<trt:GetVideoSourceConfigurationOptionsResponse><trt:Options>'
                '<tt:BoundsRange><tt:XRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:XRange>'
                '<tt:YRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:YRange>'
                f'<tt:WidthRange><tt:Min>{p.width}</tt:Min><tt:Max>{p.width}</tt:Max></tt:WidthRange>'
                f'<tt:HeightRange><tt:Min>{p.height}</tt:Min><tt:Max>{p.height}</tt:Max></tt:HeightRange>'
                '</tt:BoundsRange><tt:VideoSourceTokensAvailable>VideoSource_1'
                '</tt:VideoSourceTokensAvailable></trt:Options>'
                '</trt:GetVideoSourceConfigurationOptionsResponse>')

    def op_GetMetadataConfigurations(self, el, path):
        if "media2" in path:
            return '<tr2:GetMetadataConfigurationsResponse/>'
        return '<trt:GetMetadataConfigurationsResponse/>'

    def op_GetAudioOutputs(self, el, path):
        return '<trt:GetAudioOutputsResponse/>'

    def op_GetAudioDecoderConfigurations(self, el, path):
        return '<trt:GetAudioDecoderConfigurationsResponse/>'

    def op_GetOSDs(self, el, path):
        if "media2" in path:
            return '<tr2:GetOSDsResponse/>'
        return '<trt:GetOSDsResponse/>'

    def op_GetAnalyticsConfigurations(self, el, path):
        if "media2" in path:
            return '<tr2:GetAnalyticsConfigurationsResponse/>'
        return '<trt:GetAnalyticsConfigurationsResponse/>'

    def op_GetVideoAnalyticsConfigurations(self, el, path):
        return '<trt:GetVideoAnalyticsConfigurationsResponse/>'

    def op_GetVideoSourceModes(self, el, path):
        p = self.cam.cfg.profiles[0]
        return ('<tr2:GetVideoSourceModesResponse>'
                '<tr2:VideoSourceModes token="mode_1" Enabled="true">'
                f'<tr2:MaxFramerate>{p.fps}</tr2:MaxFramerate>'
                f'<tr2:MaxResolution><tt:Width>{p.width}</tt:Width>'
                f'<tt:Height>{p.height}</tt:Height></tr2:MaxResolution>'
                f'<tr2:Encodings>{esc(p.codec)}</tr2:Encodings>'
                '<tr2:Reboot>false</tr2:Reboot></tr2:VideoSourceModes>'
                '</tr2:GetVideoSourceModesResponse>')

    def op_GetVideoSources(self, el, path):
        p = self.cam.cfg.profiles[0]
        return ('<trt:GetVideoSourcesResponse>'
                '<trt:VideoSources token="VideoSource_1">'
                f'<tt:Framerate>{p.fps}</tt:Framerate>'
                f'<tt:Resolution><tt:Width>{p.width}</tt:Width>'
                f'<tt:Height>{p.height}</tt:Height></tt:Resolution>'
                '</trt:VideoSources></trt:GetVideoSourcesResponse>')

    def _venc2(self, p) -> str:
        """Media2 VideoEncoder2Configuration shape (tr2:Configurations)."""
        gop = p.gop or p.fps
        return (
            f'<tr2:Configurations token="venc_{esc(p.token)}" GovLength="{gop}" Profile="Main">'
            f'<tt:Name>venc_{esc(p.token)}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f'<tt:Encoding>{esc(p.codec)}</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{p.width}</tt:Width>'
            f'<tt:Height>{p.height}</tt:Height></tt:Resolution>'
            f'<tt:RateControl ConstantBitRate="false">'
            f'<tt:FrameRateLimit>{p.fps}</tt:FrameRateLimit>'
            f'<tt:BitrateLimit>{p.bitrate}</tt:BitrateLimit></tt:RateControl>'
            f'<tt:Quality>5</tt:Quality></tr2:Configurations>')

    def op_GetVideoSourceConfigurations(self, el, path):
        if "media2" in path:
            p = self.cam.cfg.profiles[0]
            return ('<tr2:GetVideoSourceConfigurationsResponse>'
                    '<tr2:Configurations token="VideoSourceConfig">'
                    '<tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>2</tt:UseCount>'
                    '<tt:SourceToken>VideoSource_1</tt:SourceToken>'
                    f'<tt:Bounds x="0" y="0" width="{p.width}" height="{p.height}"/>'
                    '</tr2:Configurations></tr2:GetVideoSourceConfigurationsResponse>')
        return ('<trt:GetVideoSourceConfigurationsResponse>'
                f'{self._vsrc_cfg()}</trt:GetVideoSourceConfigurationsResponse>')

    def op_GetVideoEncoderConfigurations(self, el, path):
        if "media2" in path:
            tok = text_of(el, "ConfigurationToken").replace("venc_", "")
            ptok = text_of(el, "ProfileToken")
            profs = [p for p in self.cam.cfg.profiles
                     if (not tok or p.token == tok) and (not ptok or p.token == ptok)]
            return ('<tr2:GetVideoEncoderConfigurationsResponse>'
                    + "".join(self._venc2(p) for p in profs)
                    + '</tr2:GetVideoEncoderConfigurationsResponse>')
        return ('<trt:GetVideoEncoderConfigurationsResponse>' + "".join(
            self._venc(p) for p in self.cam.cfg.profiles)
            + '</trt:GetVideoEncoderConfigurationsResponse>')

    def op_GetVideoEncoderConfiguration(self, el, path):
        tok = text_of(el, "ConfigurationToken").replace("venc_", "")
        p = self._profile(tok) if tok else self.cam.cfg.profiles[0]
        if p is None:
            return fault(f"No configuration '{tok}'", "ter:NoConfig", "s:Sender")
        return ('<trt:GetVideoEncoderConfigurationResponse>'
                f'{self._venc(p)}</trt:GetVideoEncoderConfigurationResponse>')

    def op_GetCompatibleVideoEncoderConfigurations(self, el, path):
        return ('<trt:GetCompatibleVideoEncoderConfigurationsResponse>' + "".join(
            self._venc(p) for p in self.cam.cfg.profiles)
            + '</trt:GetCompatibleVideoEncoderConfigurationsResponse>')

    def op_GetVideoEncoderConfigurationOptions(self, el, path):
        p = self.cam.cfg.profiles[0]
        if "media2" in path:
            return ('<tr2:GetVideoEncoderConfigurationOptionsResponse>'
                    f'<tr2:Options GovLengthRange="1 100" FrameRatesSupported="{p.fps}" '
                    'ProfilesSupported="Main" ConstantBitRateSupported="false">'
                    f'<tt:Encoding>{esc(p.codec)}</tt:Encoding>'
                    '<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>'
                    f'<tt:ResolutionsAvailable><tt:Width>{p.width}</tt:Width>'
                    f'<tt:Height>{p.height}</tt:Height></tt:ResolutionsAvailable>'
                    f'<tt:BitrateRange><tt:Min>32</tt:Min><tt:Max>{max(p.bitrate, 8192)}'
                    '</tt:Max></tt:BitrateRange></tr2:Options>'
                    '</tr2:GetVideoEncoderConfigurationOptionsResponse>')
        return ('<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>'
                '<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>'
                '<tt:H264><tt:ResolutionsAvailable>'
                f'<tt:Width>{p.width}</tt:Width><tt:Height>{p.height}</tt:Height>'
                '</tt:ResolutionsAvailable>'
                '<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>100</tt:Max></tt:GovLengthRange>'
                '<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>'
                '<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max>'
                '</tt:EncodingIntervalRange>'
                '<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported></tt:H264>'
                '</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>')

    def op_SetVideoEncoderConfiguration(self, el, path):
        return '<trt:SetVideoEncoderConfigurationResponse/>'

    def op_GetAudioSources(self, el, path):
        return '<trt:GetAudioSourcesResponse/>'

    def op_GetAudioEncoderConfigurations(self, el, path):
        return '<trt:GetAudioEncoderConfigurationsResponse/>'

    def op_GetStreamUri(self, el, path):
        p, flt = self._profile_or_fault(el)
        if flt:
            return flt
        uri = self.cam.rtsp_url(p)
        if "media2" in path:
            return f'<tr2:GetStreamUriResponse><tr2:Uri>{esc(uri)}</tr2:Uri>' \
                   f'</tr2:GetStreamUriResponse>'
        return ('<trt:GetStreamUriResponse><trt:MediaUri>'
                f'<tt:Uri>{esc(uri)}</tt:Uri>'
                '<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
                '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>'
                '<tt:Timeout>PT60S</tt:Timeout>'
                '</trt:MediaUri></trt:GetStreamUriResponse>')

    def op_GetSnapshotUri(self, el, path):
        p, flt = self._profile_or_fault(el)
        if flt:
            return flt
        uri = f"{self.base}/snapshot?profile={p.token}"
        if "media2" in path:
            return f'<tr2:GetSnapshotUriResponse><tr2:Uri>{esc(uri)}</tr2:Uri>' \
                   f'</tr2:GetSnapshotUriResponse>'
        return ('<trt:GetSnapshotUriResponse><trt:MediaUri>'
                f'<tt:Uri>{esc(uri)}</tt:Uri>'
                '<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
                '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>'
                '<tt:Timeout>PT60S</tt:Timeout>'
                '</trt:MediaUri></trt:GetSnapshotUriResponse>')

    # Media2 aliases
    def op_GetVideoEncoderConfigurationOptions2(self, el, path):
        return self.op_GetVideoEncoderConfigurationOptions(el, path)

    # -------------------------------------------------------------- events
    def op_GetEventProperties(self, el, path):
        def topic(name, inner):
            return f'<{name} wstop:topic="true">{inner}</{name}>'
        motion = topic("tns1:RuleEngine", topic("tns1:CellMotionDetector", topic(
            "tns1:Motion",
            '<tt:MessageDescription IsProperty="true">'
            '<tt:Source><tt:SimpleItemDescription Name="VideoSourceConfigurationToken" '
            'Type="tt:ReferenceToken"/>'
            '<tt:SimpleItemDescription Name="RuleName" Type="xs:string"/></tt:Source>'
            '<tt:Data><tt:SimpleItemDescription Name="IsMotion" Type="xs:boolean"/>'
            '</tt:Data></tt:MessageDescription>')))
        tamper = topic("tns1:RuleEngine", topic("tns1:TamperDetector", topic(
            "tns1:Tamper",
            '<tt:MessageDescription IsProperty="true">'
            '<tt:Data><tt:SimpleItemDescription Name="IsTamper" Type="xs:boolean"/>'
            '</tt:Data></tt:MessageDescription>')))
        loss = topic("tns1:VideoSource", topic(
            "tns1:SignalLoss",
            '<tt:MessageDescription IsProperty="true">'
            '<tt:Data><tt:SimpleItemDescription Name="State" Type="xs:boolean"/>'
            '</tt:Data></tt:MessageDescription>'))
        return ('<tev:GetEventPropertiesResponse>'
                '<tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml'
                '</tev:TopicNamespaceLocation>'
                '<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>'
                f'<wstop:TopicSet>{motion}{tamper}{loss}</wstop:TopicSet>'
                '<wsnt:TopicExpressionDialect>'
                'http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet'
                '</wsnt:TopicExpressionDialect>'
                '<tev:MessageContentFilterDialect>'
                'http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter'
                '</tev:MessageContentFilterDialect>'
                '</tev:GetEventPropertiesResponse>')

    def op_CreatePullPointSubscription(self, el, path):
        timeout = soap.duration_seconds(text_of(el, "InitialTerminationTime"), 60)
        sid = self.events.create(timeout)
        now = self.cam.now()
        addr = f"{self.base}/onvif/event_service?sub={sid}"
        return ('<tev:CreatePullPointSubscriptionResponse>'
                f'<tev:SubscriptionReference><wsa:Address>{esc(addr)}</wsa:Address>'
                '</tev:SubscriptionReference>'
                f'<wsnt:CurrentTime>{iso(now)}</wsnt:CurrentTime>'
                f'<wsnt:TerminationTime>{iso(now + timedelta(seconds=timeout))}'
                f'</wsnt:TerminationTime>'
                '</tev:CreatePullPointSubscriptionResponse>')

    async def op_PullMessages(self, el, path):
        sid = self.events.resolve(_sub_from_path(path))
        timeout = soap.duration_seconds(text_of(el, "Timeout"), 20)
        try:
            limit = int(text_of(el, "MessageLimit", "10"))
        except ValueError:
            limit = 10
        msgs = await self.events.pull(sid, min(timeout, 60), max(1, limit))
        if msgs is None:
            return fault("Subscription not found", "ter:InvalidArgVal", "s:Sender")
        now = self.cam.now()
        # report the subscription's real termination time, not a fixed 60 s:
        # a VMS schedules its Renew from this value
        term = now + timedelta(seconds=max(0.0, self.events.expires_at(sid) - time.time()))
        return ('<tev:PullMessagesResponse>'
                f'<tev:CurrentTime>{iso(now)}</tev:CurrentTime>'
                f'<tev:TerminationTime>{iso(term)}</tev:TerminationTime>'
                + "".join(msgs) + '</tev:PullMessagesResponse>')

    def op_Renew(self, el, path):
        sid = self.events.resolve(_sub_from_path(path))
        t = soap.duration_seconds(text_of(el, "TerminationTime"), 60)
        if not self.events.renew(sid, t):
            return fault("Subscription not found", "ter:InvalidArgVal", "s:Sender")
        now = self.cam.now()
        return ('<wsnt:RenewResponse>'
                f'<wsnt:CurrentTime>{iso(now)}</wsnt:CurrentTime>'
                f'<wsnt:TerminationTime>{iso(now + timedelta(seconds=t))}'
                f'</wsnt:TerminationTime></wsnt:RenewResponse>')

    def op_Unsubscribe(self, el, path):
        self.events.drop(self.events.resolve(_sub_from_path(path)))
        return '<wsnt:UnsubscribeResponse/>'

    def op_Subscribe(self, el, path):
        # base-notification push subscriptions need us to call the VMS back;
        # only PullPoint is offered (as GetCapabilities says)
        return fault("Only PullPoint subscriptions are supported",
                     "ter:ActionNotSupported", "s:Sender")

    def op_SetSynchronizationPoint(self, el, path):
        self.events.motion(False)
        return '<tev:SetSynchronizationPointResponse/>'

    # ------------------------------------------------------------- imaging
    def op_GetImagingSettings(self, el, path):
        return ('<timg:GetImagingSettingsResponse><timg:ImagingSettings>'
                '<tt:Brightness>50</tt:Brightness>'
                '<tt:ColorSaturation>50</tt:ColorSaturation>'
                '<tt:Contrast>50</tt:Contrast>'
                '<tt:Sharpness>50</tt:Sharpness>'
                '</timg:ImagingSettings></timg:GetImagingSettingsResponse>')

    def op_SetImagingSettings(self, el, path):
        return '<timg:SetImagingSettingsResponse/>'

    def op_GetOptions(self, el, path):
        rng = ('<tt:Min>0</tt:Min><tt:Max>100</tt:Max>')
        return ('<timg:GetOptionsResponse><timg:ImagingOptions>'
                f'<tt:Brightness>{rng}</tt:Brightness>'
                f'<tt:ColorSaturation>{rng}</tt:ColorSaturation>'
                f'<tt:Contrast>{rng}</tt:Contrast>'
                f'<tt:Sharpness>{rng}</tt:Sharpness>'
                '</timg:ImagingOptions></timg:GetOptionsResponse>')

    def op_GetStatus(self, el, path):
        return '<timg:GetStatusResponse><timg:Status/></timg:GetStatusResponse>'


def _sub_from_path(path: str) -> str:
    if "sub=" in path:
        return path.split("sub=", 1)[1].split("&")[0].strip()
    return ""
