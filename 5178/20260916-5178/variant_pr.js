const DEFAULT_PORT = 9374;
function parseJoinCode(input) {

  const trimmed = input.trim();
  const atIndex = trimmed.indexOf("@");

  if (atIndex === -1) {
    return { code: trimmed };
  }

  const code = trimmed.slice(0, atIndex);
  const address = trimmed.slice(atIndex + 1);

  if (!address) {
    return { code };
  }

  // Respect an explicit scheme the host typed (e.g. `ws://` for a plain-ws LAN
  // server, or a `wss://` tunnel URL). Check `wss://` first; neither prefix is a
  // prefix of the other, but the ordering keeps the intent obvious.
  let explicitScheme = null;
  let hostPort = address;
  if (address.startsWith("wss://")) {
    explicitScheme = "wss";
    hostPort = address.slice("wss://".length);
  } else if (address.startsWith("ws://")) {
    explicitScheme = "ws";
    hostPort = address.slice("ws://".length);
  }

  // Split host:port on the last colon so a host:port pair splits correctly.
  // A bracketed IPv6 literal with no port (e.g. `[::1]`) ends in `]`, and its
  // last colon is INSIDE the address — splitting there yields a broken host like
  // `[:`. When the host is a bracketed literal with no trailing `:port`, there
  // is no port to split off.
  const colonIndex = hostPort.lastIndexOf(":");
  const hasPort =
    colonIndex !== -1 &&
    colonIndex < hostPort.length - 1 &&
    !hostPort.endsWith("]");
  const host = hasPort ? hostPort.slice(0, colonIndex) : hostPort;

  const isLocal = host === "localhost" || host === "127.0.0.1";
  const scheme = explicitScheme ?? (isLocal ? "ws" : "wss");

  // No explicit port: a wss:// host is a standard-port (443) TLS endpoint; a
  // ws:// host is the phase-server default.
  let port;
  if (hasPort) {
    const parsedPort = parseInt(hostPort.slice(colonIndex + 1), 10);
    port = isNaN(parsedPort) ? DEFAULT_PORT : parsedPort;
  } else {
    port = scheme === "wss" ? 443 : DEFAULT_PORT;
  }

  // Omit the suffix when the port is the scheme's default.
  const isDefaultPort = (scheme === "wss" && port === 443) || (scheme === "ws" && port === 80);
  const portSuffix = isDefaultPort ? "" : `:${port}`;

  return {
    code,
    serverAddress: `${scheme}://${host}${portSuffix}/ws`,
  };
}
module.exports = { parseJoinCode };
