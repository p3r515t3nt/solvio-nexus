/* Deterministic JSON bytes, identical to the Core's canonical_bytes:
 * sorted keys, no whitespace, UTF-8 without ASCII escaping. Used only for
 * local delivery digests; the Core never trusts a digest computed here. */
function canonical(value){
  if(value===null||typeof value==='boolean')return JSON.stringify(value);
  if(typeof value==='number'){if(!Number.isFinite(value))throw new TypeError('non-finite number');return JSON.stringify(value);}
  if(typeof value==='string')return JSON.stringify(value);
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(typeof value==='object')return '{'+Object.keys(value).filter(key=>value[key]!==undefined).sort((a,b)=>a<b?-1:a>b?1:0)
    .map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';
  throw new TypeError('unsupported value');
}
export function canonicalText(value){return canonical(value);}
export function canonicalBytes(value){return new TextEncoder().encode(canonical(value));}
export async function sha256Hex(bytes){
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  return [...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('');
}
export async function requestDigest(value){return sha256Hex(canonicalBytes(value));}
