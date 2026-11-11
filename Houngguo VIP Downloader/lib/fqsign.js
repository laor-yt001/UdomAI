'use strict';

/**
 * Request signing for the Fanqie/Hongguo mobile-app API
 * (api5-normal-sinfonlineb.fqnovel.com). Produces the X-Gorgon, X-Helios,
 * X-Medusa, X-Ladon, X-Argus, X-Khronos and X-SS-STUB headers the app sends.
 *
 * Pure JavaScript port of the reference implementation used by yt-dlp-go
 * (extractor/hongguo). Lookup tables live in ./data.
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { sm3 } = require('./sm3');

const BRANCH_ONE_TABLE = fs.readFileSync(path.join(__dirname, 'data', 'branch_one.bin'));
const AES_SBOX = fs.readFileSync(path.join(__dirname, 'data', 'aes_sbox.bin'));

const APP_ID = '8662';
const VERSION_CODE = '74332';
const VERSION_NAME = '7.4.3.32';
const USER_AGENT =
  'com.phoenix.read/74332 (Linux; U; Android 14; zh_CN; 25053RT47C; Build/UP1A.231005.007; Cronet/TTNetVersion:8d40f833 2026-03-03 QuicVersion:21ac1950 2025-11-18)';

/* ------------------------------------------------------------------ */
/* small helpers                                                       */
/* ------------------------------------------------------------------ */

const M64 = (1n << 64n) - 1n;
const u32 = (x) => x >>> 0;
const u8 = (x) => x & 0xff;
const rotl32 = (x, n) => {
  n &= 31;
  return n === 0 ? u32(x) : u32((x << n) | (x >>> (32 - n)));
};
const rotr32 = (x, n) => rotl32(x, 32 - (n & 31));
const rotl8 = (x, n) => {
  n &= 7;
  return u8((x << n) | (x >>> (8 - n)));
};
const rotr64 = (x, n) => {
  n = BigInt(n & 63);
  if (n === 0n) return x & M64;
  return ((x >> n) | (x << (64n - n))) & M64;
};
const md5 = (b) => crypto.createHash('md5').update(b).digest();

function u32le(v) {
  const b = Buffer.alloc(4);
  b.writeUInt32LE(u32(v), 0);
  return b;
}
function u32be(v) {
  const b = Buffer.alloc(4);
  b.writeUInt32BE(u32(v), 0);
  return b;
}
function u64le(v) {
  const b = Buffer.alloc(8);
  b.writeBigUInt64LE(BigInt(v) & M64, 0);
  return b;
}
function pkcs7(b, n) {
  const p = n - (b.length % n);
  return Buffer.concat([b, Buffer.alloc(p, p)]);
}
function randomU32() {
  return crypto.randomBytes(4).readUInt32LE(0);
}
function randomDecimalId() {
  const b = crypto.randomBytes(19);
  let s = String(1 + (b[0] % 9));
  for (let i = 1; i < 19; i++) s += String(b[i] % 10);
  return s;
}

/* ------------------------------------------------------------------ */
/* query parameters                                                    */
/* ------------------------------------------------------------------ */

/** Go's url.QueryEscape with "+" -> "%20" and "%2A" -> "*". */
function queryEscape(s) {
  return encodeURIComponent(s).replace(/[!'()]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());
}

class OrderedParams {
  constructor(raw) {
    this.order = [];
    this.values = {};
    if (raw) {
      for (const part of raw.split('&')) {
        const i = part.indexOf('=');
        const k = decodeURIComponent(i < 0 ? part : part.slice(0, i));
        const v = i < 0 ? '' : decodeURIComponent(part.slice(i + 1));
        this.set(k, v);
      }
    }
  }
  set(k, v) {
    if (!Object.prototype.hasOwnProperty.call(this.values, k)) this.order.push(k);
    this.values[k] = v;
  }
  clone() {
    const c = new OrderedParams();
    c.order = this.order.slice();
    c.values = Object.assign({}, this.values);
    return c;
  }
  encode() {
    return this.order.map((k) => queryEscape(k) + '=' + queryEscape(this.values[k])).join('&');
  }
}

/* ------------------------------------------------------------------ */
/* X-Gorgon                                                            */
/* ------------------------------------------------------------------ */

function rc4Gorgon(data, key) {
  const s = new Uint8Array(256);
  for (let i = 0; i < 256; i++) s[i] = i;
  let j = 0;
  for (let i = 0; i < 256; i++) {
    j = (j + s[i] + key[i % key.length]) & 255;
    s[i] = s[j];
  }
  let i = 0;
  let jj = 0;
  const out = Buffer.alloc(data.length);
  for (let k = 0; k < data.length; k++) {
    i++;
    const x = s[i];
    jj += x;
    const y = s[jj & 255];
    s[i] = y;
    out[k] = data[k] ^ s[(y + y) & 255];
  }
  return out;
}

function reverse8(a) {
  let r = 0;
  for (let i = 0; i < 8; i++) r |= ((a >> i) & 1) << (7 - i);
  return r;
}

function encryptGorgon(body, query, khronos, xgRand) {
  const qmd5 = md5(Buffer.from(query));
  const parts = [qmd5.subarray(0, 4)];
  if (body && body.length) parts.push(md5(body).subarray(0, 4));
  else parts.push(Buffer.alloc(4));
  parts.push(Buffer.alloc(4), u32le(67503104), u32be(khronos));
  const data = Buffer.concat(parts);
  const key = Buffer.from([0x4a, 0x40, 0x16, (xgRand >> 8) & 0xff, 0x47, 0x6c, 0x01, xgRand & 0xff]);
  const out = rc4Gorgon(data, key);
  for (let i = 0; i < out.length; i++) {
    let a = out[i];
    out[i] = u8((a >> 4) | (a << 4));
    a = i + 1 < out.length ? out[i + 1] : out[0];
    a ^= out[i];
    out[i] = u8(~(reverse8(a) ^ 20));
  }
  const ret = Buffer.concat([Buffer.from([0x84, 0x04, xgRand & 0xff, (xgRand >> 8) & 0xff, 0x40, 0x01]), out]);
  return ret.toString('hex');
}

/* ------------------------------------------------------------------ */
/* X-Helios                                                            */
/* ------------------------------------------------------------------ */

function encryptHelios(khronos, r) {
  const h = md5(Buffer.concat([u32le(r), Buffer.from(APP_ID)]));
  const keysRaw = Buffer.from(h.toString('hex'), 'latin1'); // 32 ascii bytes
  const keys = [];
  for (let i = 0; i < 4; i++) keys.push(keysRaw.readBigUInt64LE(i * 8));
  const hashTable = [keys[0]];
  let b0 = keys[0];
  let b8 = keys[1];
  const queue = [keys[2], keys[3]];
  for (let i = 0; i < 0x22; i++) {
    let x = (rotr64(b8, 8) + b0) & M64;
    x ^= BigInt(i);
    queue.push(x);
    x ^= rotr64(b0, 61);
    hashTable.push(x);
    b0 = x;
    b8 = queue.shift();
  }
  const input = pkcs7(Buffer.from(`${khronos}-1588093228-${APP_ID}`), 16);
  const out = [u32le(r)];
  for (let i = 0; i < input.length; i += 16) {
    let d0 = input.readBigUInt64LE(i);
    let d1 = input.readBigUInt64LE(i + 8);
    for (let j = 0; j < 0x22; j++) {
      d1 = hashTable[j] ^ ((d0 + rotr64(d1, 8)) & M64);
      d0 = d1 ^ rotr64(d0, 61);
    }
    const b = Buffer.alloc(16);
    b.writeBigUInt64LE(d0, 0);
    b.writeBigUInt64LE(d1, 8);
    out.push(b);
  }
  return Buffer.concat(out).toString('base64');
}

/* ------------------------------------------------------------------ */
/* minimal protobuf encoder                                            */
/* ------------------------------------------------------------------ */

function varint(v) {
  let n = BigInt(v);
  const out = [];
  while (n >= 0x80n) {
    out.push(Number(n & 0x7fn) | 0x80);
    n >>= 7n;
  }
  out.push(Number(n));
  return Buffer.from(out);
}
function zigzag(v) {
  const n = BigInt(v);
  return n >= 0n ? n * 2n : -n * 2n - 1n;
}

class Proto {
  constructor() {
    this.parts = [];
  }
  tag(n, wt) {
    this.parts.push(varint((n << 3) | wt));
  }
  sint32(n, v) {
    // int32 semantics: wrap to signed 32-bit first (matches Go's int32(x)).
    this.tag(n, 0);
    this.parts.push(varint(zigzag(v | 0)));
    return this;
  }
  sint64(n, v) {
    this.tag(n, 0);
    this.parts.push(varint(zigzag(BigInt(v))));
    return this;
  }
  str(n, s) {
    if (!s) return this;
    return this.bytes(n, Buffer.from(s, 'utf8'));
  }
  bytes(n, b) {
    if (!b || !b.length) return this;
    this.tag(n, 2);
    this.parts.push(varint(b.length), Buffer.from(b));
    return this;
  }
  msg(n, p) {
    return this.bytes(n, p.buffer());
  }
  f32(n, v) {
    if (v === 0) return this;
    this.tag(n, 5);
    const b = Buffer.alloc(4);
    b.writeFloatLE(v, 0);
    this.parts.push(b);
    return this;
  }
  buffer() {
    return Buffer.concat(this.parts);
  }
}

/* ------------------------------------------------------------------ */
/* hash F13 (three branches)                                           */
/* ------------------------------------------------------------------ */

function hashIV(iv, data) {
  iv = u32(iv);
  for (let i = 0; i < data.length; i++) {
    const b = data[i];
    if ((i & 1) === 0) iv = u32((iv >>> 4) ^ iv ^ (iv << 6) ^ b);
    else iv = u32(~((iv >>> 7) ^ iv ^ (b | (iv << 12))));
  }
  return iv;
}
function hashSum(b) {
  let s = 0x20220420;
  for (let i = 0; i < 12; i++) {
    let t;
    if ((i & 1) === 0) {
      t = u32((s >>> 3) ^ s);
      s = u32(b[i] ^ (s << 7));
    } else {
      t = u32((s >>> 5) ^ s);
      s = u32(b[i] | (s << 11));
      s = u32(~s);
    }
    s = u32(s ^ t);
  }
  return u32((s | 4) ^ 0x1000000);
}
const hashFinish = (b) => Buffer.concat([b, u32le(hashSum(b))]);
function xorHalves(b) {
  const o = Buffer.alloc(16);
  for (let i = 0; i < 16; i++) o[i] = b[i] ^ b[i + 16];
  return hashFinish(o);
}

function hashF13(query, body, ts, k) {
  let iv = hashIV(0x20230928, query);
  iv = hashIV(iv, body);
  iv = hashIV(iv, ts);
  const iv0 = ((iv & 15) * 171) >>> 9;
  const branch = u32((iv & 15) - ((iv0 * 3) & 255));
  if (branch === 0) return hashBranch0(iv0, k, query, body, ts);
  if (branch === 1) return hashBranch1(iv0, k, query, body, ts);
  return hashBranch2(iv, k, query, body, ts);
}

const H0T1 = [0xc4a78580, 0xb3c0fd39, 0xc58c5686, 0xc9aa3ba7, 0xf5a7adf2, 0x963c2ed1];
const H0T2 = [
  0xebb64faf, 0x07aadcc2, 0xcf3187bf, 0xe01138ff, 0x6d0bfcff, 0x5a30a3be, 0xb41ad638, 0x34180eb8, 0xf233eb6f, 0xb1a584cc,
  0xccc30dc7, 0x47d1db51, 0xd55653de, 0x70a84fa1, 0x57473c12, 0xf76f0288, 0x2c077f0a, 0xda0dcad0, 0xfbb86f6c, 0xfdc4cf00,
  0x688a020d, 0xe676c6a6, 0x8cd6338b, 0x1a3c8d0e, 0xcce8b06b, 0x6ad0ed0b, 0xa0522717, 0xdc71ac83, 0x2285db71, 0xd5b4dda6,
  0x736f8650, 0x6560306c, 0x617ce2a6, 0xe423417e, 0x0a40e143, 0x544e4032, 0x88dffb2a, 0x716c1ae0, 0x4c467a88, 0x05b23bb3,
  0xe1d0b866, 0xbaa3dcb8, 0xae3374d3, 0xc3381a50, 0x1702f75b, 0xfe6da368, 0xf0b4cf48, 0x4e0ffbb8, 0x72aad10d, 0x26c53a3d,
  0xf2bce0f6, 0xb4557581, 0x4a257fdd, 0x8c3182a2, 0xab0b3b86, 0x3d5dfb14, 0x4f103634, 0xd37b52d7, 0x444eff16, 0xeb0a33d1,
  0x6ca86f6e, 0x00284ba7, 0x08387cfa, 0x5fb37586,
];
const H0_ARGS = [
  [101, 5, 7, 6, 3, 2, 1, 0, 5, 4, 3],
  [96, 0, 6, 7, 5, 3, 2, 1, 5, 4, 4],
  [96, 7, 6, 2, 1, 4, 0, 5, 4, 3, 5],
  [99, 3, 6, 2, 4, 5, 1, 0, 0, 7, 6],
  [96, 0, 5, 6, 7, 3, 1, 2, 5, 4, 4],
  [100, 2, 0, 3, 5, 4, 6, 7, 2, 1, 5],
];

function hashBranch0(iv0, k, q, b, t) {
  const base = H0T1[iv0];
  const c1 = u8(base + k + 1);
  const c2 = u32(base + k);
  const tt = H0T2.map((x) => rotr32(x, c1));
  const n = (c2 + 2) & 7;
  const seed = [0xfa, 0x45, 0x61, 0xd7];
  const pad = Buffer.from(seed.map((x) => u8((x * 257) >> n)));
  const data = Buffer.concat([q, b, t, pad, Buffer.from([0, 0, 0, 0, 0, 0, 1, 0xa0])]);
  const di = new Array(128).fill(0);
  for (let i = 0; i < Math.floor(data.length / 4); i++) di[i] = data.readUInt32BE(i * 4);
  let d0 = di[0];
  for (let i = 0; i < 112; i++) {
    const d1 = di[i + 1];
    const d14 = di[i + 14];
    d0 = u32(d0 + di[i + 9] + u32(rotl32(d1, 14) ^ rotl32(d1, 25) ^ (d1 >>> 3)) + u32(rotl32(d14, 13) ^ rotl32(d14, 15) ^ (d14 >>> 10)));
    di[i + 16] = d0;
    d0 = d1;
  }
  const cc = u8(c2);
  const init = [0x7aba4fc8, 0x67166507, 0x6403fa00, 0x340f512f, 984304912, 3005047866, 2874125293, 2152413264].map((x) => rotr32(x, cc));
  const d = hashBranch0Rounds(init, base, di, tt, H0_ARGS[iv0]);
  const out = Buffer.alloc(32);
  for (let i = 0; i < 8; i++) out.writeUInt32BE(u32(d[i] + init[i]), i * 4);
  return xorHalves(out);
}
function hashBranch0Rounds(src, base, di, tt, a) {
  let d = src.slice();
  for (let i = 0; i < a[0]; i++) {
    const n0 = di[u32(base + i) & 127];
    const n1 = u32(((d[a[3]] ^ d[a[4]]) & d[a[1]]) ^ d[a[3]]);
    const n2 = u32(rotl32(d[a[1]], 26) ^ rotl32(d[a[1]], 21) ^ rotl32(d[a[1]], 7));
    const n4 = u32(n0 + n1 + n2 + tt[u32(base + i) & 63] + d[a[5]]);
    const n5 = u32(rotl32(d[a[2]], 30) ^ rotl32(d[a[2]], 19) ^ rotl32(d[a[2]], 10));
    const n6 = u32((d[a[2]] & d[a[6]]) | ((d[a[2]] | d[a[6]]) & d[a[7]]));
    const o = d[a[9]];
    d = [d[7], d[0], d[1], d[2], d[3], d[4], d[5], d[6]];
    d[a[10]] = u32(n5 + n6 + n4);
    d[a[8]] = u32(o + n4);
  }
  return d;
}

const H1_ORDERS = [5, 7, 1, 2, 4, 0, 6, 3, 0, 5, 2, 4, 1, 3, 7, 6, 5, 7, 2, 4, 1, 6, 3, 0, 3, 0, 2, 4, 6, 7, 1, 5, 4, 5, 0, 3, 6, 2, 1, 7];
const H1_CONSTS = [
  0x87aeea5dab37cd6bn, 0x7ff48becb4f54087n, 0xb0724c06706bbd5dn, 0x1fe5dfb1143e328dn, 0x1a2331d00af4f1f2n, 0xcaff7131bb1e71ban,
  0x33385e1042752218n, 0xff01ed65d4a441fbn, 0x0adb1ec8828c80e8n, 0x62475d12f4e06fe7n, 0xbd0b238da4fe72n,
];

function hashBranch1(iv0, k, q, b, t) {
  const base = [0x808a9c79, 0xf079807e, 0xbadf79c5, 0xa785d3ff, 0x82d8438c][iv0];
  const c = rotr64(BigInt(u32(k)), u8(base + k));
  const order = H1_ORDERS.slice(iv0 * 8, iv0 * 8 + 8);
  const rot = u32(base + k + 1) & 63;
  const tt = H1_CONSTS.map((x) => rotr64(x, rot));
  const data = Buffer.concat([q, b, t, Buffer.from([0x80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])]);
  const src = [];
  for (let i = 0; i < 8; i++) src.push(data.readBigUInt64BE(i * 8));
  let sx = [c, c, c, c, c, c, c, c];
  const table = BRANCH_ONE_TABLE.subarray(iv0 << 14, (iv0 << 14) + 16384);
  let swap = hashSwap(src, sx, tt, table, order, null);
  const zeros = [0n, 0n, 0n, 0n, 0n, 0n, 0n, 416n];
  sx = [swap[4], swap[3], swap[2], swap[1], swap[0], swap[7], swap[6], swap[5]];
  swap = hashSwap(zeros, sx, tt, table, order, swap);
  const out = Buffer.alloc(32);
  let n = 0;
  for (let i = 7; i >= 0; i--) {
    const x = Buffer.alloc(8);
    x.writeBigUInt64LE(swap[i], 0);
    out[n] = x[1];
    out[n + 1] = x[3];
    out[n + 2] = x[5];
    out[n + 3] = x[6];
    n += 4;
  }
  return xorHalves(out);
}
const tableAt = (tab, x) => tab.readBigUInt64LE(Number(x));
function hashR00(r, tt, tab) {
  let x = tableAt(tab, 8n * (r[0] >> 56n)) ^ tt;
  x ^= tableAt(tab, ((r[1] >> 45n) & 2040n) + 2048n);
  x ^= tableAt(tab, ((r[2] >> 37n) & 2040n) + 4096n);
  x ^= tableAt(tab, ((r[3] >> 29n) & 2040n) + 6144n);
  x ^= tableAt(tab, ((r[4] >> 21n) & 2040n) + 8192n);
  x ^= tableAt(tab, ((r[5] >> 13n) & 2040n) + 10240n);
  x ^= tableAt(tab, ((r[6] >> 8n) & 255n) * 8n + 12288n);
  x ^= tableAt(tab, (r[7] & 255n) * 8n + 14336n);
  return x;
}
function rotate8(a, n) {
  const r = new Array(8);
  for (let i = 0; i < 8; i++) r[i] = a[(i + n) & 7];
  return r;
}
function hashSwap(src, sx, tt, tab, order, typ) {
  let da = new Array(8);
  let ha = sx.slice();
  for (let i = 0; i < 8; i++) da[i] = src[order[i]] ^ sx[i];
  for (let round = 0; round < 10; round++) {
    const rr = new Array(8);
    for (let i = 0; i < 8; i++) rr[i] = hashR00(rotate8(ha, i), 0n, tab);
    rr[7] ^= tt[round + 1];
    const old = da.slice();
    for (let i = 0; i < 8; i++) da[i] = hashR00(rotate8(old, i), rr[i], tab);
    ha = rr;
  }
  if (!typ) {
    src[0] = da[0] ^ sx[0] ^ src[0];
    for (let i = 1; i < 8; i++) src[i] = da[8 - i] ^ sx[i] ^ src[i];
  } else {
    for (let i = 0; i < 6; i++) src[i] = da[7 - i] ^ typ[i + 1];
    src[6] = da[1] ^ typ[7] ^ src[7];
    src[7] = da[0] ^ typ[0];
  }
  return src;
}

const MD_SV = [
  0xa7aefe20, 0x7149f1d6, 0x47e4ca07, 0xe9b58f67, 0x93b924de, 0xc614d0f5, 0x38afe0ef, 0xb2bbad73, 0xe24444c3, 0x9d3aec9b,
  0xdf7b37e4, 0xd8b16d40, 0xf8ac31b8, 0x76b9a90b, 0x31d833ee, 0x953fce64, 0x353595a4, 0x4609c13b, 0x36925008, 0x8c6d0925,
  0x5df5c177, 0x1cfbf52b, 0x8a4fa7f0, 0x114ca35e, 0x8193f984, 0x7a7a8733, 0x316ab4d5, 0x3c20cfc9, 0xa6d84453, 0x3a18500c,
  0x798ec47a, 0x97a76b28, 0x66c4ff96, 0x51716443, 0x0dd2fc3b, 0xb5696da7, 0xbbeb3ac5, 0x5c53d204, 0xd32608ce, 0x7279b9ec,
  0xf4188ecf, 0xf7d793db, 0x332cc491, 0xab76ae15, 0x9bebe727, 0x18a01384, 0x5be9f8a7, 0x5f90a754, 0x39b663c0, 0x36673c83,
  0x7c92f514, 0x9d7d94d7, 0xe2e8d9aa, 0x5f7e9ea9, 0x7abd4551, 0x569e05da, 0x40a25632, 0x3df5a9a5, 0xbab37d80, 0x454286dc,
  0x3f5d4e78, 0x03d7b75d, 0xb1fe4af7, 0xa5ab26a3,
];
const BRANCH2_ORDERS = [
  15, 7, 4, 0, 9, 8, 3, 10, 6, 11, 5, 13, 14, 1, 12, 2, 15, 5, 8, 12, 0, 9, 2, 1, 3, 7, 14, 6, 11, 10, 13, 4, 6, 5, 0, 7, 12, 0, 10, 4, 8,
  15, 1, 11, 13, 9, 2, 14, 6, 11, 2, 5, 4, 3, 8, 1, 1, 7, 10, 0, 13, 12, 9, 14, 13, 7, 14, 15, 11, 2, 8, 3, 12, 5, 9, 1, 0, 4, 6, 10, 13,
  9, 2, 6, 15, 11, 10, 4, 8, 7, 0, 12, 5, 3, 1, 14, 12, 9, 15, 7, 6, 15, 3, 14, 2, 13, 4, 5, 1, 11, 10, 0, 12, 5, 10, 9, 14, 8, 2, 4, 4,
  7, 3, 15, 1, 6, 11, 0, 11, 15, 4, 8, 2, 10, 7, 0, 9, 13, 6, 1, 14, 3, 5, 12, 11, 6, 10, 5, 8, 2, 12, 3, 7, 15, 14, 9, 13, 0, 1, 4, 9,
  6, 8, 15, 5, 8, 0, 4, 10, 11, 3, 13, 1, 2, 12, 14, 9, 13, 12, 6, 4, 7, 10, 3, 3, 15, 0, 8, 1, 5, 2, 14, 1, 0, 13, 15, 9, 10, 11, 14, 4,
  2, 8, 7, 3, 6, 12, 5, 1, 8, 10, 12, 15, 9, 5, 6, 11, 0, 3, 4, 2, 14, 7, 13, 4, 8, 15, 0, 12, 15, 14, 13, 10, 1, 6, 2, 7, 9, 5, 3, 4, 2,
  5, 8, 13, 11, 10, 6, 6, 0, 14, 15, 7, 12, 9, 3, 10, 8, 4, 15, 0, 11, 1, 6, 13, 12, 7, 9, 3, 14, 5, 2, 10, 7, 11, 5, 15, 0, 2, 14, 1, 8,
  3, 13, 12, 6, 9, 4, 13, 7, 15, 8, 5, 15, 6, 4, 11, 10, 14, 12, 9, 0, 2, 3, 13, 12, 2, 7, 4, 1, 11, 14, 14, 8, 6, 15, 9, 5, 0, 3,
];

function hashBranch2(iv, k, q, b, t) {
  const n = u32(((iv & 15) - 2) * 86);
  const iv0 = ((n >>> 15) & 255) + ((n >>> 8) & 255);
  const base = [0x8980f29b, 0xeb549c7f, 0xb08726db, 0xd40cb5e6, 0xe8f559e4][iv0];
  const c1 = u8(base + k + 1);
  const c2 = u32(base + k);
  const sh = (c2 + 5) & 7;
  const seed = [0x84, 0x96, 0x77, 0x9d, 0xd4, 0x15, 0x0b, 0xf8];
  const pad = Buffer.from(seed.map((x) => u8((x * 257) >> sh)));
  const data = Buffer.concat([q, b, t, pad, Buffer.from([0xa0, 1, 0, 0])]);
  return customMD5(data, c2, BRANCH2_ORDERS.slice(iv0 * 64, iv0 * 64 + 64), c1);
}
function customMD5(msg, c, ord, c1) {
  const sv = MD_SV.map((x) => rotr32(x, c1));
  const cc = u8(c);
  const start = [0x79e0f2fb, 0xc8b52570, 0xebc2f8cd, 0x7c104d93].map((x) => rotr32(x, cc));
  const ec = u8(c + 6);
  const end = [0x19be4866, 0xe85986b4, 0xe19b326e, 0x71d1d7d4].map((x) => rotr32(x, ec));
  const m = [];
  for (let i = 0; i < 16; i++) m.push(msg.readUInt32LE(i * 4));
  let a = start[0];
  let b = start[1];
  let cx = start[2];
  let d = start[3];
  const shifts = [
    [7, 12, 17, 22],
    [5, 9, 14, 20],
    [4, 11, 16, 23],
    [6, 10, 15, 21],
  ];
  for (let r = 0; r < 4; r++) {
    for (let i = 0; i < 16; i++) {
      let f;
      if (r === 0) f = (b & cx) | (~b & d);
      else if (r === 1) f = (b & d) | (cx & ~d);
      else if (r === 2) f = b ^ cx ^ d;
      else f = cx ^ (b | ~d);
      f = u32(f);
      const nv = u32(b + rotl32(u32(a + f + m[ord[r * 16 + i]] + sv[r * 16 + i]), shifts[r][i & 3]));
      a = d;
      d = cx;
      cx = b;
      b = nv;
    }
  }
  const out = Buffer.alloc(16);
  const vals = [u32(u32(start[0] + a) ^ end[0]), u32(u32(start[1] + b) ^ end[1]), u32(u32(start[2] + cx) ^ end[2]), u32(u32(start[3] + d) ^ end[3])];
  for (let i = 0; i < 4; i++) out.writeUInt32LE(vals[i], i * 4);
  return hashFinish(out);
}

/* ------------------------------------------------------------------ */
/* "AES v3" block transform used to wrap X-Medusa                      */
/* ------------------------------------------------------------------ */

class AesV3 {
  constructor(key, k) {
    const w = k & 3;
    this.word = w;
    this.sbox = AES_SBOX.subarray(w * 256, w * 256 + 256);
    this.con2 = [
      [1, 0, 2, 3],
      [2, 0, 3, 1],
      [0, 1, 3, 2],
      [1, 0, 2, 3],
    ][w];
    this.order = [
      [0, 9, 14, 11, 4, 13, 2, 7, 8, 1, 6, 15, 12, 5, 10, 3],
      [0, 9, 14, 15, 4, 13, 2, 7, 8, 1, 6, 3, 12, 5, 10, 11],
      [0, 9, 14, 7, 4, 13, 2, 11, 8, 1, 6, 3, 12, 5, 10, 15],
      [0, 9, 14, 11, 4, 13, 2, 7, 8, 1, 6, 15, 12, 5, 10, 3],
    ][w];
    this.keys = [];
    this.expand(key);
  }
  expand(key) {
    const init = [0xca025ddc, 0x823dc546, 0xc9420583, 0xc298225f][this.word];
    const mk = Buffer.alloc(48);
    for (let i = 0; i < 4; i++) mk.writeUInt32LE(init, i * 4);
    for (let i = 0; i < 16; i++) mk[i] ^= key[i];
    let rounds = 8;
    for (let i = 4; i < 12; i++) {
      const idx = 4 * (i - 1);
      let k0 = mk[idx];
      let k1 = mk[idx + 1];
      let k2 = mk[idx + 2];
      let k3 = mk[idx + 3];
      if ((i & 3) === 0) {
        const k00 = u8(init >>> (rounds & 24)) ^ this.sbox[k1];
        const n1 = this.sbox[k2];
        const n2 = this.sbox[k3];
        const n3 = this.sbox[k0];
        k1 = n1;
        k2 = n2;
        k3 = n3;
        k0 = k00;
      }
      rounds += 2;
      mk[idx + 4] = k0 ^ mk[idx - 12];
      mk[idx + 5] = k1 ^ mk[idx - 11];
      mk[idx + 6] = k2 ^ mk[idx - 10];
      mk[idx + 7] = k3 ^ mk[idx - 9];
    }
    for (let i = 0; i < 12; i++) this.keys.push(Array.from(mk.subarray(i * 4, i * 4 + 4)));
  }
  encrypt(data, iv) {
    const plain = this.sumData(data);
    let prev = Buffer.from(iv);
    const key = [];
    for (let i = 0; i < plain.length; i += 16) {
      const x = Buffer.alloc(16);
      for (let j = 0; j < 16; j++) x[j] = plain[i + j] ^ prev[j];
      const blk = this.block(x);
      for (const b of blk) key.push(b);
      prev = blk;
    }
    const out = this.mix(data, key);
    return Buffer.concat([Buffer.from([key[key.length - 1]]), out]);
  }
  sumData(d) {
    const k = Buffer.alloc(32);
    for (let i = 0; i < 31; i++) {
      const x = i * 8;
      k[i] = u8(
        ((d[x] >> 4) & 2) |
          (d[x + 1] & 64) |
          ((d[x + 2] >> 2) & 1) |
          ((d[x + 3] << 3) & 128) |
          ((d[x + 4] >> 1) & 4) |
          ((d[x + 5] << 3) & 16) |
          ((d[x + 6] << 5) & 32) |
          ((d[x + 7] >> 4) & 8)
      );
    }
    k[31] = 1;
    return k;
  }
  mix(d, key) {
    const o = Buffer.from(d);
    for (let i = 0; i < 31; i++) {
      const k = key[i];
      const x = i * 8;
      o[x] = u8((o[x] & ~32) | ((k << 4) & 32));
      o[x + 1] = u8((o[x + 1] & ~64) | (k & 64));
      o[x + 2] = u8((o[x + 2] & ~4) | ((k * 4) & 4));
      o[x + 3] = u8((o[x + 3] & ~16) | ((k >> 3) & 16));
      o[x + 4] = u8((o[x + 4] & ~8) | ((k + k) & 8));
      o[x + 5] = u8((o[x + 5] & ~2) | ((k >> 3) & 2));
      o[x + 6] = u8((o[x + 6] & ~1) | ((k >> 5) & 1));
      o[x + 7] = u8((o[x + 7] & 127) | ((k << 4) & 128));
    }
    return o;
  }
  block(p) {
    let s = [0, 1, 2, 3].map((i) => Array.from(p.subarray(i * 4, i * 4 + 4)));
    const c2 = this.con2;
    const addCon = (st, keys) => {
      for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) st[i][j] ^= keys[i][c2[j]];
    };
    addCon(s, this.keys.slice(0, 4));
    for (let r = 1; r < 3; r++) {
      for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) s[i][j] = this.sbox[s[i][j]];
      s = [s[c2[0]], s[c2[1]], s[c2[2]], s[c2[3]]];
      const bs = [].concat(...s);
      for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) s[i][j] = bs[this.order[i * 4 + j]];
      if (r === 1) {
        for (let i = 0; i < 4; i++) s[i] = [s[i][c2[0]], s[i][c2[1]], s[i][c2[2]], s[i][c2[3]]];
        mixCols(s);
      }
      addCon(s, this.keys.slice(r * 4, r * 4 + 4));
    }
    for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) s[i][j] ^= this.keys[4 + i][j];
    return Buffer.from([].concat(...s));
  }
}
function xtime(x) {
  return u8(x & 128 ? (x << 1) ^ 0x1b : x << 1);
}
function mixCols(s) {
  for (let i = 0; i < 4; i++) {
    const t = s[0][i] ^ s[1][i] ^ s[2][i] ^ s[3][i];
    const u = s[0][i];
    s[0][i] ^= t ^ xtime(s[0][i] ^ s[1][i]);
    s[1][i] ^= t ^ xtime(s[1][i] ^ s[2][i]);
    s[2][i] ^= t ^ xtime(s[2][i] ^ s[3][i]);
    s[3][i] ^= t ^ xtime(s[3][i] ^ u);
  }
}

/* ------------------------------------------------------------------ */
/* X-Medusa                                                            */
/* ------------------------------------------------------------------ */

function medusaKeyHash(key, r) {
  const b = Buffer.alloc(68);
  key.copy(b, 0);
  b.writeUInt32LE(u32(r), 32);
  key.copy(b, 36);
  const h = sm3(b);
  const d1 = (r >>> 16) & 255;
  let d2 = u32((d1 << 11) | (r >>> 24));
  d2 = u32(d2 ^ ((d1 >>> 5) ^ d1));
  d2 = u32(~d2);
  return { key: h, seed: u32le(d2) };
}
function medusaXMXor(data, key) {
  const n = data.length;
  const o = Buffer.alloc(n);
  for (let i = 0; i < n; i++) {
    const v = data[i];
    const idx = (i * 4) & 28;
    const d0 = key[idx];
    const d1 = key[idx + 1];
    let d2 = u8(rotl8(v, 4) + d0) ^ d1;
    d2 = u8(~d2);
    d2 = rotl8(d2, 3);
    d2 = u8(d2 + d1);
    d2 ^= d0;
    o[n - 1 - i] = u8(~d2);
  }
  const last = o[n - 1] ^ o[n - 2];
  const d0 = o[0];
  o[0] = u8(o[0] + u8(~last));
  o[1] = u8(o[1] + (o[0] ^ o[n - 1] ^ 254));
  o[2] = u8(o[2] + (u8(last - d0) ^ rotl8(o[1], 3) ^ 2));
  for (let i = 0; i < n - 4; i++) {
    const temp = rotl8(o[i + 2], 3) ^ o[i + 1] ^ u8(i + 3);
    o[i + 3] = u8(o[i + 3] + u8(~temp));
  }
  o[n - 1] ^= o[n - 2];
  let sum = 0;
  for (let i = 0; i < n - 1; i++) sum += o[i + 1];
  o[0] = u8((o[0] ^ o[1]) + sum);
  return o;
}

const MEDUSA_MAGIC = Buffer.from([0xf7, 0xe8, 0x5f, 0xfa, 0xd7, 0xd7, 0xdc, 0x3b, 0xd6, 0x2a, 0xc8, 0x70, 0x57, 0xcf, 0x61, 0x18]);
const MEDUSA_KEY32 = Buffer.from([
  0x8e, 0xbd, 0xfa, 0x38, 0x06, 0xec, 0xc5, 0xce, 0xe7, 0x94, 0x23, 0xe6, 0x02, 0x9e, 0xd8, 0x25, 0x40, 0xbc, 0x22, 0x18, 0xbb, 0x7e, 0xae, 0xf7,
  0x1c, 0xb6, 0x91, 0xf7, 0xaa, 0x8a, 0xa2, 0xf5,
]);
const AESV3_KEY = Buffer.from([0xf1, 0x59, 0x33, 0x76, 0x76, 0x6e, 0xa9, 0x8d, 0x34, 0xf3, 0x1b, 0x05, 0x7a, 0x9d, 0x5b, 0xe4]);
const AESV3_IV = Buffer.from([0x1f, 0xe1, 0x09, 0xa4, 0x12, 0x52, 0x83, 0xf4, 0x18, 0xde, 0x9e, 0x05, 0x1a, 0x96, 0x9e, 0x12]);

function generateMedusa(signedUrl, query, body, dev, khronos, reportTime, sr) {
  const qsm3 = sm3(Buffer.from(signedUrl.split('?').slice(1).join('?')));
  const bodyMD5 = body && body.length ? md5(body) : Buffer.alloc(16);
  const ts = u32le(khronos);
  const qbody = hashF13(qsm3, bodyMD5, ts, khronos);
  const qbHash = sm3(Buffer.concat([Buffer.from(query), bodyMD5, Buffer.from('none')]));

  const device = new Proto()
    .sint32(1, 1)
    .sint32(2, 2)
    .str(3, APP_ID)
    .str(4, dev.deviceId)
    .str(5, 'Ai6svO3PyrwDOUSmO6ZcResxu')
    .str(6, '!noperm!')
    .sint32(7, -888888)
    .sint32(8, -888888)
    .sint32(9, 3)
    .sint32(10, -888888)
    .str(11, '!notset!')
    .str(12, 'Asia/Shanghai,8')
    .str(13, 'zh_CN')
    .sint32(14, 4)
    .f32(16, 255.24993896484375)
    .f32(17, 35.58599090576172)
    .f32(18, 3.467449188232422)
    .f32(19, 3.467449188232422)
    .f32(20, 255.1754913330078)
    .f32(21, 42.17544174194336)
    .str(22, '14')
    .sint32(23, 41)
    .sint32(24, 34)
    .sint64(25, 1728388016635n)
    .sint64(26, 1728388016635n)
    .sint64(27, 1728388016635n)
    .sint64(28, 1728388016637n)
    .sint32(29, -1)
    .str(30, '25053RT47C')
    .str(31, 'Redmi')
    .str(32, '25053RT47C')
    .str(33, '25053RT47C')
    .str(34, 'Xiaomi')
    .str(35, 'Redmi')
    .str(36, 'Redmi')
    .sint32(38, 31);
  const report = new Proto().sint64(1, BigInt(reportTime)).sint32(2, -2).sint32(4, 200);
  const env = new Proto()
    .sint32(1, sr.launch)
    .sint32(2, 146331399)
    .sint32(3, 146331396)
    .sint32(5, 7)
    .str(6, 'v04.06.04.03-bugfix')
    .sint32(7, sr.pid)
    .msg(12, device)
    .msg(13, report)
    .str(14, VERSION_NAME);
  const req = new Proto().sint32(1, 111).sint32(2, 10).sint32(3, 694367).sint32(5, 586952199);
  const proto = new Proto()
    .bytes(1, MEDUSA_MAGIC)
    .sint32(2, 3)
    .sint32(3, sr.proto)
    .str(4, APP_ID)
    .str(5, dev.deviceId)
    .str(6, '1588093228')
    .str(7, VERSION_NAME)
    .str(8, 'v04.06.04-ml-android')
    .sint32(9, 67503104)
    .bytes(10, u64le(320))
    .sint32(12, khronos)
    .bytes(13, qbody)
    .bytes(14, qsm3.subarray(0, 6))
    .msg(15, req)
    .str(16, 'AXYQOS6n2m60x1fVZHIrH3iol')
    .sint32(17, khronos)
    .bytes(19, qbHash)
    .str(20, 'none')
    .sint32(21, 312)
    .msg(23, env)
    .str(24, '{"cmr":16777216,"cmr2":16777216,"un_h":1879194040,"vpn":0,"kd":0,"fkd":3672518972,"pd":-1872573247,"dyn":"","do":0,"tk":true}')
    .buffer();

  const { key, seed } = medusaKeyHash(MEDUSA_KEY32, sr.hash);
  let data = medusaXMXor(proto, key);
  data = Buffer.concat([u64le(320), data]);
  data.reverse();
  for (let i = 0; i < data.length; i++) data[i] ^= seed[~i & 3];
  const check = u32(0x18000001 | ((qsm3[0] & 63) << 14) | ((qbody[0] & 63) << 8));
  const payload = Buffer.concat([Buffer.from([0x35]), u32le(sr.xm), u32le(check), data, u32le(sr.hash).subarray(2)]);
  const aesOut = new AesV3(AESV3_KEY, khronos).encrypt(payload, AESV3_IV);
  const version = Buffer.concat([Buffer.from([0x03, 0, 0, 0]), MEDUSA_MAGIC]);
  for (let i = 0; i < version.length; i += 4) version.writeUInt32LE(u32(version.readUInt32LE(i) ^ khronos), i);
  const out = Buffer.concat([version, u32le(sr.hash).subarray(0, 2), Buffer.from([0, 1]), aesOut]);
  return out.toString('base64');
}

/* ------------------------------------------------------------------ */
/* public API                                                          */
/* ------------------------------------------------------------------ */

function newSignRandom() {
  return {
    gorgon: randomU32() & 0xffff,
    helios: randomU32(),
    proto: randomU32(),
    hash: randomU32(),
    xm: randomU32(),
    launch: 100 + (randomU32() % 21),
    pid: 10001 + (randomU32() % 2000),
  };
}

/**
 * Sign a request. `params` is an OrderedParams (without ts/_rticket),
 * `body` the exact JSON bytes that will be POSTed.
 * @returns {{url:string, headers:Object<string,string>}}
 */
function signRequest(base, params, body, dev, now = new Date(), sr = newSignRandom()) {
  const q = params.clone();
  const unix = Math.floor(now.getTime() / 1000);
  q.set('ts', String(unix));
  q.set('_rticket', String(now.getTime()));
  const query = q.encode();
  const signedUrl = base + '?' + query;
  const khronos = u32(unix);
  const gorgon = encryptGorgon(body, query, khronos, sr.gorgon);
  const helios = encryptHelios(khronos, sr.helios);
  const medusa = generateMedusa(signedUrl, query, body, dev, khronos, unix, sr);
  const headers = {
    'User-Agent': USER_AGENT,
    Accept: 'application/json; charset=utf-8,application/x-protobuf',
    'Content-Type': 'application/json; charset=UTF-8',
    'x-xs-from-web': '0',
    'x-ss-req-ticket': String(now.getTime()),
    'x-tt-request-tag': 't=0;n=0',
    'sdk-version': '2',
    'passport-sdk-version': '50561',
    'x-vc-bdturing-sdk-version': '3.7.2.cn',
    'x-ladon': u32be(khronos).toString('base64'),
    'x-khronos': String(khronos),
    'x-argus': u32le(khronos).toString('base64'),
    'x-gorgon': gorgon,
    'x-helios': helios,
    'x-medusa': medusa,
    'x-ss-stub': md5(body || Buffer.alloc(0)).toString('hex').toUpperCase(),
  };
  return { url: signedUrl, headers };
}

module.exports = {
  APP_ID,
  VERSION_CODE,
  VERSION_NAME,
  USER_AGENT,
  OrderedParams,
  signRequest,
  newSignRandom,
  randomDecimalId,
  // exported for tests
  encryptGorgon,
  encryptHelios,
  generateMedusa,
  hashF13,
  sm3,
};
