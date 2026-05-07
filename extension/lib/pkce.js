// PKCE helpers (RFC 7636) using Web Crypto. No deps.

function base64url(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomState(byteLen = 32) {
  const buf = new Uint8Array(byteLen);
  crypto.getRandomValues(buf);
  return base64url(buf);
}

export async function makePkcePair() {
  const verifier = randomState(32);
  const data = new TextEncoder().encode(verifier);
  const digest = await crypto.subtle.digest("SHA-256", data);
  const challenge = base64url(new Uint8Array(digest));
  return { verifier, challenge };
}
