//! API to convert WireGuard components <=> telio components

use ipnetwork::{IpNetwork, IpNetworkError};
use serde::{Deserialize, Serialize};
use telio_crypto::{KeyDecodeError, PublicKey, SecretKey};
use telio_model::mesh::{Node, NodeState};
use telio_utils::telio_log_warn;
use wireguard_uapi::{get, xplatform::set};

use std::{
    collections::BTreeMap,
    fmt::{self, Display, Formatter},
    io::{BufRead, BufReader, Read},
    net::{AddrParseError, SocketAddr},
    num::ParseIntError,
    panic,
    str::FromStr,
    sync::Arc,
    time::{Duration, Instant, SystemTime, SystemTimeError, UNIX_EPOCH},
};

/// Error types from uapi responses
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum Error {
    #[error("Parsing of '{0}' failed: {1}")]
    /// Error is a parsing error
    ParsingError(&'static str, String),
}

/// telio implementation of wireguard::Peer
#[derive(Debug, Default, PartialEq, Eq, Clone)]
pub struct Peer {
    /// Public key, the peer's primary identifier
    pub public_key: PublicKey,
    /// Peer's endpoint with `IP address` and `UDP port` number
    pub endpoint: Option<SocketAddr>,
    /// Keep alive interval, `seconds` or `None`
    pub persistent_keepalive_interval: Option<u32>,
    /// Vector of allowed IPs
    pub allowed_ips: Vec<IpNetwork>,
    /// Number of bytes received or `None`(unused on Set)
    pub rx_bytes: Option<u64>,
    /// Number of bytes transmitted or `None`(unused on Set)
    pub tx_bytes: Option<u64>,
    /// Time since last handshakeor `None`, differs from WireGuard field meaning
    pub time_since_last_handshake: Option<Duration>,
}

impl From<get::Peer> for Peer {
    /// Convert from WireGuard get::Peer to telio Peer
    fn from(item: get::Peer) -> Self {
        Self {
            public_key: PublicKey(item.public_key),
            endpoint: item.endpoint,
            persistent_keepalive_interval: Some(item.persistent_keepalive_interval.into()),
            allowed_ips: item
                .allowed_ips
                .into_iter()
                .map(|ip| IpNetwork::new(ip.ipaddr, ip.cidr_mask))
                .collect::<Result<Vec<IpNetwork>, _>>()
                .unwrap_or_default(),
            rx_bytes: Some(item.rx_bytes),
            tx_bytes: Some(item.tx_bytes),
            time_since_last_handshake: Peer::calculate_time_since_last_handshake(Some(
                item.last_handshake_time,
            )),
        }
    }
}

impl From<set::Peer> for Peer {
    /// Convert from WireGuard set::Peer to telio Peer
    fn from(item: set::Peer) -> Self {
        Self {
            public_key: PublicKey(item.public_key),
            endpoint: item.endpoint,
            persistent_keepalive_interval: item.persistent_keepalive_interval.map(u32::from),
            allowed_ips: item
                .allowed_ips
                .into_iter()
                .map(|ip| IpNetwork::new(ip.ipaddr, ip.cidr_mask))
                .collect::<Result<Vec<IpNetwork>, _>>()
                .unwrap_or_default(),
            ..Default::default()
        }
    }
}

impl From<&Node> for Peer {
    fn from(other: &Node) -> Peer {
        Peer {
            public_key: other.public_key,
            allowed_ips: other.allowed_ips.clone(),
            endpoint: other.endpoint,
            persistent_keepalive_interval: Some(25),
            ..Default::default()
        }
    }
}

impl From<&Peer> for Node {
    fn from(other: &Peer) -> Self {
        Self {
            public_key: other.public_key,
            allowed_ips: other.allowed_ips.clone(),
            endpoint: other.endpoint,
            ..Default::default()
        }
    }
}

impl From<&Event> for Node {
    fn from(other: &Event) -> Node {
        Self {
            public_key: other.peer.public_key,
            state: other.state,
            allowed_ips: other.peer.allowed_ips.clone(),
            endpoint: other.peer.endpoint,
            ..Default::default()
        }
    }
}

impl From<&Peer> for set::Peer {
    /// Convert from telio Peer to WireGuard set::Peer
    fn from(item: &Peer) -> Self {
        Self {
            public_key: item.public_key.0,
            endpoint: item.endpoint,
            persistent_keepalive_interval: item.persistent_keepalive_interval.map(|x| x as u16),
            allowed_ips: item
                .allowed_ips
                .iter()
                .map(|ip| set::AllowedIp {
                    ipaddr: ip.network(),
                    cidr_mask: ip.prefix(),
                })
                .collect(),
            ..Default::default()
        }
    }
}

/// telio-wg representation of WireGuard Interface
#[derive(Debug, Default, PartialEq, Eq, Clone)]
pub struct Interface {
    /// Private key or `None`
    pub private_key: Option<SecretKey>,
    /// Listen port or `None`
    pub listen_port: Option<u16>,
    /// firewall mark
    pub fwmark: u32,
    /// Dictionary of Peer-s
    pub peers: BTreeMap<PublicKey, Peer>,
}

impl From<get::Device> for Interface {
    /// Convert from wireguard get::Device to telio Interface
    fn from(item: get::Device) -> Self {
        Self {
            private_key: item.private_key.map(SecretKey::new),
            listen_port: Some(item.listen_port),
            fwmark: item.fwmark,
            peers: item
                .peers
                .into_iter()
                .map(|p| (PublicKey(p.public_key), Peer::from(p)))
                .collect(),
        }
    }
}

impl From<set::Device> for Interface {
    /// Convert from wireguard set::Device to telio Interface
    fn from(item: set::Device) -> Self {
        Self {
            private_key: item.private_key.map(SecretKey::new),
            listen_port: item.listen_port,
            fwmark: item.fwmark.map_or(0, |x| x),
            peers: item
                .peers
                .into_iter()
                .map(|p| (PublicKey(p.public_key), Peer::from(p)))
                .collect(),
        }
    }
}

impl From<Interface> for set::Device {
    /// Convert from telio Interface to wireguard set::Device
    fn from(item: Interface) -> Self {
        Self {
            private_key: item.private_key.map(|key| key.into_bytes()),
            listen_port: item.listen_port,
            fwmark: match item.fwmark {
                0 => None,
                x => Some(x),
            },
            peers: item.peers.values().map(Into::<set::Peer>::into).collect(),
            ..Default::default()
        }
    }
}

/// Types of commands
#[derive(Debug, PartialEq)]
pub enum Cmd {
    /// Get command has no underlying structure
    Get,
    /// Set command is wrapping WireGuard set::Device
    Set(set::Device),
}

/// Response type of [UAPI](https://www.wireguard.com/xplatform/) requests
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Response {
    /// The error code of the response. '0' denotes no error
    pub errno: i32,
    /// Contains an interface if there is no error, otherwise 'None'
    pub interface: Option<Interface>,
}

/// The connection state of the Node
pub type PeerState = NodeState;

/// Peer information to transmit
#[derive(Debug, PartialEq, Eq)]
pub struct Event {
    /// The state of the Peer
    pub state: PeerState,
    /// Details regarding the Peer
    pub peer: Peer,
}

/// Analytics information to be conveyed
#[derive(Clone, Debug)]
pub struct AnalyticsEvent {
    /// Public key of the Peer
    pub public_key: PublicKey,
    /// IP address and port number of the socket
    pub endpoint: SocketAddr,
    /// Number of transmitted bytes
    pub tx_bytes: u64,
    /// Number of recieved bytes
    pub rx_bytes: u64,
    /// State of the Peer
    pub peer_state: PeerState,
    /// Timestamp of the event
    pub timestamp: Instant,
}

impl Peer {
    /// Represents 2022-03-04 17:00:05
    #[cfg(test)]
    const MOCK_UNIX_TIME: Duration = Duration::from_secs(1646405984);

    /// Checks whether the Peer is still connected.
    /// Returns 'false' if there has been no response from
    /// Peer for some time.
    pub fn is_connected(&self) -> bool {
        // https://web.archive.org/web/20200603205723/https://www.wireguard.com/papers/wireguard.pdf
        // 6.1
        const REJECT_AFTER_TIME: Duration = Duration::from_secs(180);
        // Whenever a handshake initiation message is sent as the result of an
        // expiring timer, an additional amount of jitter is added to the
        // expiration, in order to prevent two peers from repeatedly initiating
        // handshakes at the same time.
        //
        // ernestask: Canonical implementations use a third of a second, but at
        //            least wireguard-go rounds up.
        const REKEY_TIMEOUT_JITTER: Duration = Duration::from_millis(334);
        // 6.2
        // However, for the case in which a peer has received data but does not
        // have any data to send back immediately, and the Reject-After-Time
        // second deadline is approaching in sooner than Keepalive-Timeout
        // seconds, then the initiation triggered by an aged secure session
        // occurs during the receive path.
        //
        // 6.4
        // After sending a handshake initiation message, because of a
        // first-packet condition, or because of the limit conditions of
        // section 6.2, if a handshake response message (section 5.4.3) is not
        // subsequently received after Rekey-Timeout seconds, a new handshake
        // initiation message is constructed (with new random ephemeral keys)
        // and sent. This reinitiation is attempted for Rekey-Attempt-Time
        // seconds before giving up, though this counter is reset when a peer
        // explicitly attempts to send a new transport data message.
        //
        // ernestask: With the above in mind, the hard deadline for rekeying is
        //            Reject-After-Time - Rekey-Timeout + Rekey-Attempt-Time,
        //            although wireguard-go seems to implement it in a way that
        //            ends up being Reject-After-Time + Rekey-Attempt-Time.
        //
        //            However, since this mostly pertains to judging whether
        //            the peer is connect_ed_ vs connect_ing_, simply using
        //            Reject-After-Time + jitter should be fine.

        self.time_since_last_handshake
            .map_or(false, |d| d < REJECT_AFTER_TIME + REKEY_TIMEOUT_JITTER)
    }

    /// Returns the current state of the peer
    pub fn state(&self) -> PeerState {
        if self.is_connected() {
            PeerState::Connected
        } else {
            PeerState::Connecting
        }
    }

    /// Detects changes in endpoints and allowed ips
    pub fn is_same_event(&self, other: &Self) -> bool {
        (&self.public_key, &self.endpoint, &self.allowed_ips)
            == (&self.public_key, &other.endpoint, &other.allowed_ips)
    }

    #[cfg(not(test))]
    fn get_unix_time() -> Result<Duration, SystemTimeError> {
        SystemTime::now().duration_since(UNIX_EPOCH)
    }

    #[cfg(test)]
    fn get_unix_time() -> Result<Duration, SystemTimeError> {
        Ok(Self::MOCK_UNIX_TIME)
    }

    /// Convert uapi last_handshake_time into Duration since handshake
    pub fn calculate_time_since_last_handshake(lht: Option<Duration>) -> Option<Duration> {
        // 0 means no handshake
        let lht = lht.and_then(|handshake_time| {
            if handshake_time == Duration::from_secs(0) {
                None
            } else {
                Some(handshake_time)
            }
        });

        lht.and_then(|handshake_time| match Self::get_unix_time() {
            Ok(now) => now.checked_sub(handshake_time),
            Err(e) => {
                telio_log_warn!("Failed to parse unix_time for peer: {}", e);
                None
            }
        })
    }
}

impl Display for Cmd {
    fn fmt(&self, f: &mut Formatter) -> fmt::Result {
        match self {
            Cmd::Get => writeln!(f, "get=1"),
            Cmd::Set(interface) => {
                writeln!(f, "set=1")?;
                writeln!(f, "{}", interface)
            }
        }
    }
}

impl Display for Response {
    fn fmt(&self, f: &mut Formatter) -> fmt::Result {
        match self.errno {
            e if e != 0 => {
                writeln!(f, "error={}", e)?;
            }
            _ => (),
        }
        Ok(())
    }
}

pub(super) fn response_from_str(string: &str) -> Result<Response, Error> {
    response_from_read(string.as_bytes())
}

#[allow(unwrap_check)]
pub(super) fn response_from_read<R: Read>(reader: R) -> Result<Response, Error> {
    let mut reader = BufReader::new(reader);
    let mut interface = Interface::default();
    let mut inited = false;
    let mut errno = 0;
    let mut cmd = String::new();

    while reader.read_line(&mut cmd).is_ok() {
        cmd.pop(); // remove newline if any
        if cmd.is_empty() {
            return Ok(Response {
                errno,
                interface: if inited { Some(interface) } else { None },
            }); // Done
        }
        {
            let parsed: Vec<&str> = cmd.splitn(2, '=').collect();
            if parsed.len() != 2 {
                return Err(Error::ParsingError("cmd", "not in A=B format".to_owned()));
            }
            let (key, val) = (
                *parsed
                    .first()
                    .ok_or_else(|| Error::ParsingError("cmd", "No key found".to_owned()))?,
                *parsed
                    .get(1)
                    .ok_or_else(|| Error::ParsingError("cmd", "No val found".to_owned()))?,
            );

            match key {
                "private_key" => {
                    inited = true;
                    interface.private_key = Some(val.parse().map_err(|e: KeyDecodeError| {
                        Error::ParsingError("private_key", e.to_string())
                    })?);
                }
                "listen_port" => {
                    inited = true;
                    let port = val.parse().map_err(|e: ParseIntError| {
                        Error::ParsingError("listen_port", e.to_string())
                    })?;
                    if port > 0 {
                        interface.listen_port = Some(port);
                    }
                }
                "fwmark" => {
                    inited = true;
                    interface.fwmark = val
                        .parse()
                        .map_err(|e: ParseIntError| Error::ParsingError("fwmark", e.to_string()))?;
                }
                "public_key" => {
                    inited = true;
                    let mut public = val.parse().map_err(|e: KeyDecodeError| {
                        Error::ParsingError("public_key", e.to_string())
                    })?;
                    loop {
                        let (peer, next, err) = parse_peer(public, &mut reader)?;
                        let _ = interface.peers.insert(peer.public_key, peer);
                        if let Some(err) = err {
                            errno = err;
                            break;
                        }
                        if let Some(next) = next {
                            public = next;
                        } else {
                            break;
                        }
                    }
                }
                "errno" => {
                    errno = val
                        .parse()
                        .map_err(|e: ParseIntError| Error::ParsingError("errno", e.to_string()))?
                }
                _ => (),
            }
        }
        cmd.clear();
    }

    Ok(Response {
        errno,
        interface: if inited { Some(interface) } else { None },
    })
}

#[allow(unwrap_check)]
fn parse_peer<R: Read>(
    public_key: PublicKey,
    reader: &mut BufReader<R>,
) -> Result<(Peer, Option<PublicKey>, Option<i32>), Error> {
    let mut cmd = String::new();

    let mut peer = Peer {
        public_key,
        ..Peer::default()
    };

    let mut last_handshake_time = None;
    let mut resp =
        loop {
            if reader.read_line(&mut cmd).is_err() {
                break (peer, None, None);
            }

            cmd.pop(); // remove newline if any
            if cmd.is_empty() {
                break (peer, None, None);
            }

            let parsed: Vec<&str> = cmd.splitn(2, '=').collect();
            if parsed.len() != 2 {
                return Err(Error::ParsingError("cmd", "not in A=B format".to_owned()));
            }
            let (key, val) = (
                *parsed
                    .first()
                    .ok_or_else(|| Error::ParsingError("cmd", "No key found".to_owned()))?,
                *parsed
                    .get(1)
                    .ok_or_else(|| Error::ParsingError("cmd", "Invalid value".to_owned()))?,
            );

            match key {
                "endpoint" => {
                    peer.endpoint = Some(val.parse().map_err(|e: AddrParseError| {
                        Error::ParsingError("endpoint", e.to_string())
                    })?)
                }
                "persistent_keepalive_interval" => {
                    peer.persistent_keepalive_interval =
                        Some(val.parse().map_err(|e: ParseIntError| {
                            Error::ParsingError("persistent_keepalive_interval", e.to_string())
                        })?);
                }
                "allowed_ip" => {
                    peer.allowed_ips
                        .push(val.parse().map_err(|e: IpNetworkError| {
                            Error::ParsingError("allowed_ip", e.to_string())
                        })?)
                }
                "rx_bytes" => {
                    peer.rx_bytes = Some(val.parse().map_err(|e: ParseIntError| {
                        Error::ParsingError("rx_bytes", e.to_string())
                    })?)
                }
                "tx_bytes" => {
                    peer.tx_bytes = Some(val.parse().map_err(|e: ParseIntError| {
                        Error::ParsingError("tx_bytes", e.to_string())
                    })?)
                }
                "last_handshake_time_nsec" => {
                    let nsec = Duration::from_nanos(val.parse().map_err(|e: ParseIntError| {
                        Error::ParsingError("last_handshake_time_nsec", e.to_string())
                    })?);
                    if let Some(ref mut timestamp) = last_handshake_time {
                        *timestamp += nsec;
                    } else {
                        last_handshake_time = Some(nsec);
                    }
                }
                "last_handshake_time_sec" => {
                    let sec = Duration::from_secs(val.parse().map_err(|e: ParseIntError| {
                        Error::ParsingError("last_handshake_time_sec", e.to_string())
                    })?);
                    if let Some(ref mut timestamp) = last_handshake_time {
                        *timestamp += sec;
                    } else {
                        last_handshake_time = Some(sec);
                    }
                }
                "public_key" => {
                    break (
                        peer,
                        Some(val.parse().map_err(|e: KeyDecodeError| {
                            Error::ParsingError("public_key", e.to_string())
                        })?), // Indicate next peer's public
                        None,
                    );
                }
                "errno" => {
                    break (
                        peer,
                        None,
                        Some(val.parse().map_err(|e: ParseIntError| {
                            Error::ParsingError("errno", e.to_string())
                        })?),
                    )
                }
                _ => (),
            }
            cmd.clear();
        };

    resp.0.time_since_last_handshake =
        Peer::calculate_time_since_last_handshake(last_handshake_time);

    Ok(resp)
}

#[cfg(test)]
mod tests {
    use super::*;

    use pretty_assertions::assert_eq;

    trait PeerHelp {
        fn peer_map(self) -> BTreeMap<PublicKey, Peer>;
    }

    impl PeerHelp for Vec<Peer> {
        fn peer_map(self) -> BTreeMap<PublicKey, Peer> {
            self.into_iter().map(|p| (p.public_key, p)).collect()
        }
    }

    #[test]
    fn bytes_data_overflow() -> Result<(), Error> {
        let sk1 = SecretKey::gen();
        let pk1 = hex::encode(sk1.public());
        let pk2 = hex::encode(SecretKey::gen().public());
        let sk1 = hex::encode(sk1.as_bytes());
        let base_string = format!(
            "\
private_key={sk1}
listen_port=12912
public_key={pk1}
endpoint=[abcd:23::33%2]:51820
allowed_ip=192.168.4.4/32
allowed_ip=fd74:656c:696::beef:4/128
last_handshake_time_nsec=1234
last_handshake_time_sec=1
public_key={pk2}
rx_bytes=RXBYTES
tx_bytes=TXBYTES
last_handshake_time_nsec=51204
last_handshake_time_sec=100
endpoint=182.122.22.19:3233
persistent_keepalive_interval=111
allowed_ip=192.168.4.10/32
allowed_ip=192.168.4.11/32
allowed_ip=fd74:656c:696f::ceed:3/128
errno=0
"
        );

        let received_bytes_overflow_string = base_string
            .replace("RXBYTES", "1000000000000")
            .replace("TXBYTES", "100"); // more than 4gb received in total
        let sent_bytes_overflow_string = base_string
            .replace("RXBYTES", "100")
            .replace("TXBYTES", "1000000000000"); // more than 4gb sent in total

        response_from_str(&received_bytes_overflow_string)?;
        response_from_str(&sent_bytes_overflow_string)?;
        Ok(())
    }

    #[test]
    fn zero_listen_port_becomes_none() {
        let resp_str = "\
listen_port=0
errno=0
";
        let resp = Response {
            errno: 0,
            interface: Some(Interface::default()),
        };
        assert_eq!(response_from_str(&resp_str), Ok(resp));
    }
}
