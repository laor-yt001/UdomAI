'use strict';

/**
 * ISO Common Encryption ('cenc', AES-128-CTR) decryption for MP4 files,
 * pure Node.js. Handles both plain (moov+mdat) and fragmented (moof/mdat)
 * files, full-sample and subsample encryption, 8- or 16-byte per-sample IVs.
 *
 * The whole file is loaded into memory, samples are decrypted in place, the
 * encrypted sample entries (encv/enca) are rewritten to their original codec
 * and the protection boxes are turned into 'free' boxes. The result plays in
 * any player.
 */

const crypto = require('crypto');
const fsp = require('fs/promises');

const CONTAINERS = new Set(['moov', 'trak', 'mdia', 'minf', 'stbl', 'moof', 'traf', 'mvex', 'sinf', 'schi', 'edts', 'dinf', 'udta']);

function* boxes(buf, start, end) {
  let p = start;
  while (p + 8 <= end) {
    let size = buf.readUInt32BE(p);
    const type = buf.toString('latin1', p + 4, p + 8);
    let hdr = 8;
    if (size === 1) {
      if (p + 16 > end) break;
      size = Number(buf.readBigUInt64BE(p + 8));
      hdr = 16;
    } else if (size === 0) {
      size = end - p;
    }
    if (size < hdr || p + size > end) break;
    yield { type, start: p, size, hdr, body: p + hdr, end: p + size };
    p += size;
  }
}
const children = (buf, b) => Array.from(boxes(buf, b.body, b.end));
const child = (buf, b, type) => children(buf, b).find((c) => c.type === type) || null;
const path = (buf, b, ...types) => {
  let cur = b;
  for (const t of types) {
    cur = cur && child(buf, cur, t);
  }
  return cur || null;
};
const fullFlags = (buf, b) => buf.readUInt32BE(b.body) & 0xffffff;
const fullVersion = (buf, b) => buf[b.body];

/* ------------------------------------------------------------------ */
/* moov parsing                                                        */
/* ------------------------------------------------------------------ */

function parseTenc(buf, b) {
  const v = fullVersion(buf, b);
  let p = b.body + 4;
  p += 1; // reserved
  let cryptByteBlock = 0;
  let skipByteBlock = 0;
  if (v === 0) p += 1;
  else {
    cryptByteBlock = buf[p] >> 4;
    skipByteBlock = buf[p] & 15;
    p += 1;
  }
  const isProtected = buf[p++];
  const ivSize = buf[p++];
  const kid = buf.subarray(p, p + 16).toString('hex');
  p += 16;
  let constIv = null;
  if (isProtected && ivSize === 0) {
    const n = buf[p++];
    constIv = Buffer.from(buf.subarray(p, p + n));
  }
  return { isProtected, ivSize, kid, constIv, cryptByteBlock, skipByteBlock };
}

/** Find a child box by scanning for its type tag (sample entries have variable-length fixed fields). */
function scanForBox(buf, start, end, type) {
  let idx = buf.indexOf(type, start, 'latin1');
  while (idx >= 0 && idx + 4 <= end) {
    if (idx >= start + 4) {
      const size = buf.readUInt32BE(idx - 4);
      if (size >= 8 && idx - 4 + size <= end) return { type, start: idx - 4, size, hdr: 8, body: idx + 4, end: idx - 4 + size };
    }
    idx = buf.indexOf(type, idx + 4, 'latin1');
  }
  return null;
}

function parseStsd(buf, stsd) {
  const count = buf.readUInt32BE(stsd.body + 4);
  const entries = [];
  let i = 0;
  for (const e of boxes(buf, stsd.body + 8, stsd.end)) {
    if (i++ >= count) break;
    const entry = { box: e, type: e.type, encrypted: false, sinf: null, frma: null, scheme: null, tenc: null };
    if (['encv', 'enca', 'enct', 'encs'].includes(e.type)) {
      const sinf = scanForBox(buf, e.body, e.end, 'sinf');
      if (sinf) {
        entry.sinf = sinf;
        const frma = child(buf, sinf, 'frma');
        const schm = child(buf, sinf, 'schm');
        const tenc = path(buf, sinf, 'schi', 'tenc');
        entry.frma = frma ? buf.toString('latin1', frma.body, frma.body + 4) : null;
        entry.scheme = schm ? buf.toString('latin1', schm.body + 4, schm.body + 8) : null;
        entry.tenc = tenc ? parseTenc(buf, tenc) : null;
        entry.encrypted = !!(entry.tenc && entry.tenc.isProtected);
      }
    }
    entries.push(entry);
  }
  return entries;
}

function readTable(buf, b, width) {
  const count = buf.readUInt32BE(b.body + 4);
  const out = new Array(count);
  let p = b.body + 8;
  for (let i = 0; i < count; i++) {
    out[i] = width === 8 ? Number(buf.readBigUInt64BE(p)) : buf.readUInt32BE(p);
    p += width;
  }
  return out;
}

function parseStbl(buf, stbl) {
  const r = { stsd: null, entries: [], sizes: null, defaultSize: 0, sampleCount: 0, stsc: [], chunks: null, saiz: null, saio: null, senc: null, boxes: children(buf, stbl) };
  for (const b of r.boxes) {
    switch (b.type) {
      case 'stsd':
        r.stsd = b;
        r.entries = parseStsd(buf, b);
        break;
      case 'stsz': {
        r.defaultSize = buf.readUInt32BE(b.body + 4);
        r.sampleCount = buf.readUInt32BE(b.body + 8);
        if (r.defaultSize === 0) {
          r.sizes = new Array(r.sampleCount);
          for (let i = 0; i < r.sampleCount; i++) r.sizes[i] = buf.readUInt32BE(b.body + 12 + i * 4);
        }
        break;
      }
      case 'stsc': {
        const n = buf.readUInt32BE(b.body + 4);
        for (let i = 0; i < n; i++) {
          const p = b.body + 8 + i * 12;
          r.stsc.push({ firstChunk: buf.readUInt32BE(p), perChunk: buf.readUInt32BE(p + 4), sdi: buf.readUInt32BE(p + 8) });
        }
        break;
      }
      case 'stco':
        r.chunks = readTable(buf, b, 4);
        break;
      case 'co64':
        r.chunks = readTable(buf, b, 8);
        break;
      case 'saiz':
        r.saiz = parseSaiz(buf, b);
        break;
      case 'saio':
        r.saio = parseSaio(buf, b);
        break;
      case 'senc':
        r.senc = b;
        break;
      default:
        break;
    }
  }
  return r;
}

function parseSaiz(buf, b) {
  const flags = fullFlags(buf, b);
  let p = b.body + 4;
  let auxType = null;
  if (flags & 1) {
    auxType = buf.toString('latin1', p, p + 4);
    p += 8;
  }
  const defaultSize = buf[p++];
  const count = buf.readUInt32BE(p);
  p += 4;
  const sizes = new Array(count);
  for (let i = 0; i < count; i++) sizes[i] = defaultSize || buf[p + i];
  return { auxType, defaultSize, count, sizes, box: b };
}
function parseSaio(buf, b) {
  const v = fullVersion(buf, b);
  const flags = fullFlags(buf, b);
  let p = b.body + 4;
  let auxType = null;
  if (flags & 1) {
    auxType = buf.toString('latin1', p, p + 4);
    p += 8;
  }
  const count = buf.readUInt32BE(p);
  p += 4;
  const offsets = new Array(count);
  for (let i = 0; i < count; i++) {
    offsets[i] = v === 1 ? Number(buf.readBigUInt64BE(p)) : buf.readUInt32BE(p);
    p += v === 1 ? 8 : 4;
  }
  return { auxType, offsets, box: b };
}

function trackId(buf, trak) {
  const tkhd = child(buf, trak, 'tkhd');
  if (!tkhd) return null;
  return fullVersion(buf, tkhd) === 1 ? buf.readUInt32BE(tkhd.body + 4 + 16) : buf.readUInt32BE(tkhd.body + 4 + 8);
}

function parseMoov(buf, moov) {
  const tracks = new Map();
  const trexDefaults = new Map();
  for (const trak of children(buf, moov).filter((b) => b.type === 'trak')) {
    const id = trackId(buf, trak);
    const stbl = path(buf, trak, 'mdia', 'minf', 'stbl');
    if (id == null || !stbl) continue;
    const t = parseStbl(buf, stbl);
    t.id = id;
    t.trak = trak;
    t.encEntry = t.entries.find((e) => e.encrypted) || null;
    tracks.set(id, t);
  }
  const mvex = child(buf, moov, 'mvex');
  if (mvex) {
    for (const trex of children(buf, mvex).filter((b) => b.type === 'trex')) {
      trexDefaults.set(buf.readUInt32BE(trex.body + 4), { defaultSize: buf.readUInt32BE(trex.body + 16) });
    }
  }
  return { tracks, trexDefaults };
}

/* ------------------------------------------------------------------ */
/* sample auxiliary info (IVs + subsample map)                         */
/* ------------------------------------------------------------------ */

/** Parse `count` sample-info records at `pos`. `sizes` (from saiz) is optional; senc-style uses `subsampleFlag`. */
function parseAuxRecords(buf, pos, count, ivSize, { sizes = null, subsampleFlag = null } = {}) {
  const out = [];
  let p = pos;
  for (let i = 0; i < count; i++) {
    const startP = p;
    const iv = ivSize ? Buffer.from(buf.subarray(p, p + ivSize)) : null;
    p += ivSize;
    let subsamples = null;
    const hasSub = subsampleFlag != null ? subsampleFlag : sizes ? sizes[i] > ivSize : false;
    if (hasSub) {
      const n = buf.readUInt16BE(p);
      p += 2;
      subsamples = new Array(n);
      for (let j = 0; j < n; j++) {
        subsamples[j] = { clear: buf.readUInt16BE(p), enc: buf.readUInt32BE(p + 2) };
        p += 6;
      }
    }
    if (sizes && sizes[i]) p = startP + sizes[i];
    out.push({ iv, subsamples });
  }
  return out;
}

function parseSenc(buf, senc, ivSize) {
  const flags = fullFlags(buf, senc);
  const count = buf.readUInt32BE(senc.body + 4);
  return parseAuxRecords(buf, senc.body + 8, count, ivSize, { subsampleFlag: !!(flags & 2) });
}

/* ------------------------------------------------------------------ */
/* decryption                                                          */
/* ------------------------------------------------------------------ */

function decryptSample(buf, off, size, key, iv, subsamples) {
  if (off < 0 || off + size > buf.length) throw new Error(`sample at ${off} (+${size}) lies outside the file`);
  const iv16 = Buffer.alloc(16);
  if (iv) iv.copy(iv16, 0, 0, Math.min(16, iv.length));
  const d = crypto.createDecipheriv('aes-128-ctr', key, iv16);
  if (!subsamples || !subsamples.length) {
    d.update(buf.subarray(off, off + size)).copy(buf, off);
    return;
  }
  let p = off;
  const end = off + size;
  for (const s of subsamples) {
    p += s.clear;
    if (s.enc) {
      if (p + s.enc > end) throw new Error('subsample map exceeds sample size');
      d.update(buf.subarray(p, p + s.enc)).copy(buf, p);
      p += s.enc;
    }
  }
}

function rename(buf, b, type) {
  buf.write(type, b.start + 4, 4, 'latin1');
}

/** Turn the protection boxes in a stbl or traf into 'free' boxes. */
function neutralizeAux(buf, container) {
  for (const b of children(buf, container)) {
    if (b.type === 'senc' || b.type === 'saiz' || b.type === 'saio') rename(buf, b, 'free');
    else if ((b.type === 'sbgp' || b.type === 'sgpd') && buf.toString('latin1', b.body + 4, b.body + 8) === 'seig') rename(buf, b, 'free');
  }
}

/**
 * Decrypt a CENC MP4 in memory.
 * @param {Buffer} buf whole file (modified in place)
 * @param {Buffer} key 16 bytes
 * @param {(done:number,total:number)=>void} [onProgress]
 * @returns {{encrypted:boolean, samples:number, fragments:number}}
 */
function decryptBuffer(buf, key, onProgress) {
  const top = Array.from(boxes(buf, 0, buf.length));
  const moov = top.find((b) => b.type === 'moov');
  if (!moov) throw new Error('not an MP4 file (no moov box)');
  const { tracks, trexDefaults } = parseMoov(buf, moov);
  const encTracks = [...tracks.values()].filter((t) => t.encEntry);
  if (!encTracks.length) return { encrypted: false, samples: 0, fragments: 0 };
  for (const t of encTracks) {
    const scheme = t.encEntry.scheme;
    if (scheme && scheme !== 'cenc') throw new Error(`unsupported encryption scheme '${scheme}' (only 'cenc' AES-CTR is supported)`);
    if (t.encEntry.tenc.ivSize === 0 && !t.encEntry.tenc.constIv) throw new Error('tenc has neither per-sample nor constant IV');
  }

  let samples = 0;
  let fragments = 0;
  let processed = 0;
  const total = buf.length;
  const tick = () => {
    if (onProgress) onProgress(Math.min(processed, total), total);
  };

  // --- non-fragmented samples (stco/co64 + stsz/stsc) ---
  for (const t of encTracks) {
    if (!t.chunks || !t.chunks.length || !t.sampleCount) continue;
    const { ivSize, constIv } = t.encEntry.tenc;
    const list = [];
    let s = 0;
    for (let c = 0; c < t.chunks.length && s < t.sampleCount; c++) {
      const chunkNo = c + 1;
      let per = 0;
      for (const e of t.stsc) if (e.firstChunk <= chunkNo) per = e.perChunk;
      let off = t.chunks[c];
      for (let i = 0; i < per && s < t.sampleCount; i++, s++) {
        const size = t.sizes ? t.sizes[s] : t.defaultSize;
        list.push({ off, size });
        off += size;
      }
    }
    let aux = null;
    if (t.senc) aux = parseSenc(buf, t.senc, ivSize);
    else if (t.saiz && t.saio && t.saio.offsets.length) {
      if (t.saio.offsets.length === 1) {
        aux = parseAuxRecords(buf, t.saio.offsets[0], t.saiz.count, ivSize, { sizes: t.saiz.sizes });
      } else {
        // one offset per chunk
        aux = [];
        let idx = 0;
        for (let c = 0; c < t.chunks.length; c++) {
          const chunkNo = c + 1;
          let per = 0;
          for (const e of t.stsc) if (e.firstChunk <= chunkNo) per = e.perChunk;
          const n = Math.min(per, t.saiz.count - idx);
          if (n <= 0) break;
          aux.push(...parseAuxRecords(buf, t.saio.offsets[Math.min(c, t.saio.offsets.length - 1)], n, ivSize, { sizes: t.saiz.sizes.slice(idx, idx + n) }));
          idx += n;
        }
      }
    }
    if (!aux) throw new Error(`track ${t.id}: encrypted samples without senc/saio auxiliary info`);
    const n = Math.min(list.length, aux.length);
    for (let i = 0; i < n; i++) {
      decryptSample(buf, list[i].off, list[i].size, key, aux[i].iv || constIv, aux[i].subsamples);
      processed += list[i].size;
      samples++;
      if ((i & 63) === 0) tick();
    }
  }

  // --- fragmented samples (moof/traf/trun + senc or saiz/saio) ---
  for (const moof of top.filter((b) => b.type === 'moof')) {
    fragments++;
    let prevTrafEnd = moof.start;
    for (const traf of children(buf, moof).filter((b) => b.type === 'traf')) {
      const tfhdBox = child(buf, traf, 'tfhd');
      if (!tfhdBox) continue;
      const tf = parseTfhd(buf, tfhdBox);
      const track = tracks.get(tf.trackId);
      const trex = trexDefaults.get(tf.trackId) || {};
      let base;
      if (tf.baseDataOffset != null) base = tf.baseDataOffset;
      else if (tf.flags & 0x20000) base = moof.start;
      else base = prevTrafEnd;
      const list = [];
      let pos = base;
      const truns = children(buf, traf).filter((b) => b.type === 'trun');
      for (const trunBox of truns) {
        const tr = parseTrun(buf, trunBox);
        if (tr.dataOffset != null) pos = base + tr.dataOffset;
        for (const sz of tr.sizes) {
          const size = sz != null ? sz : tf.defaultSize != null ? tf.defaultSize : trex.defaultSize || 0;
          list.push({ off: pos, size });
          pos += size;
        }
      }
      prevTrafEnd = pos;
      if (!track || !track.encEntry) continue;
      const { ivSize, constIv } = track.encEntry.tenc;
      const sencBox = child(buf, traf, 'senc');
      let aux = null;
      if (sencBox) aux = parseSenc(buf, sencBox, ivSize);
      else {
        const saizBox = child(buf, traf, 'saiz');
        const saioBox = child(buf, traf, 'saio');
        if (saizBox && saioBox) {
          const saiz = parseSaiz(buf, saizBox);
          const saio = parseSaio(buf, saioBox);
          if (saio.offsets.length) aux = parseAuxRecords(buf, base + saio.offsets[0], saiz.count, ivSize, { sizes: saiz.sizes });
        }
      }
      if (!aux) throw new Error(`fragment at ${moof.start}: encrypted track ${tf.trackId} has no senc/saio info`);
      const n = Math.min(list.length, aux.length);
      for (let i = 0; i < n; i++) {
        decryptSample(buf, list[i].off, list[i].size, key, aux[i].iv || constIv, aux[i].subsamples);
        processed += list[i].size;
        samples++;
      }
      neutralizeAux(buf, traf);
    }
    tick();
  }

  if (!samples) throw new Error('found encrypted tracks but no samples to decrypt');

  // --- rewrite the movie header so players see a clear file ---
  for (const t of encTracks) {
    for (const e of t.entries) {
      if (!e.sinf) continue;
      if (e.frma) buf.write(e.frma, e.box.start + 4, 4, 'latin1');
      rename(buf, e.sinf, 'free');
    }
    neutralizeAux(buf, path(buf, t.trak, 'mdia', 'minf', 'stbl'));
  }
  for (const b of children(buf, moov)) if (b.type === 'pssh') rename(buf, b, 'free');
  processed = total;
  tick();
  return { encrypted: true, samples, fragments };
}

function parseTfhd(buf, b) {
  const flags = fullFlags(buf, b);
  let p = b.body + 4;
  const r = { flags, trackId: buf.readUInt32BE(p), baseDataOffset: null, sdi: null, defaultSize: null };
  p += 4;
  if (flags & 1) {
    r.baseDataOffset = Number(buf.readBigUInt64BE(p));
    p += 8;
  }
  if (flags & 2) {
    r.sdi = buf.readUInt32BE(p);
    p += 4;
  }
  if (flags & 8) p += 4;
  if (flags & 0x10) {
    r.defaultSize = buf.readUInt32BE(p);
    p += 4;
  }
  return r;
}
function parseTrun(buf, b) {
  const flags = fullFlags(buf, b);
  let p = b.body + 4;
  const count = buf.readUInt32BE(p);
  p += 4;
  const r = { flags, dataOffset: null, sizes: new Array(count) };
  if (flags & 1) {
    r.dataOffset = buf.readInt32BE(p);
    p += 4;
  }
  if (flags & 4) p += 4;
  for (let i = 0; i < count; i++) {
    if (flags & 0x100) p += 4;
    if (flags & 0x200) {
      r.sizes[i] = buf.readUInt32BE(p);
      p += 4;
    } else r.sizes[i] = null;
    if (flags & 0x400) p += 4;
    if (flags & 0x800) p += 4;
  }
  return r;
}

/** Quick check: does this MP4 contain CENC-protected sample entries? */
function isEncrypted(buf) {
  const moov = Array.from(boxes(buf, 0, buf.length)).find((b) => b.type === 'moov');
  if (!moov) return false;
  const { tracks } = parseMoov(buf, moov);
  return [...tracks.values()].some((t) => t.encEntry);
}

/**
 * Decrypt `inPath` into `outPath`.
 * @param {string} inPath
 * @param {string} outPath
 * @param {string|Buffer} key 16-byte key (hex string or Buffer)
 * @param {{onProgress?:(done:number,total:number)=>void}} [opts]
 */
async function decryptFile(inPath, outPath, key, opts = {}) {
  const k = Buffer.isBuffer(key) ? key : Buffer.from(String(key).replace(/^0x/i, ''), 'hex');
  if (k.length !== 16) throw new Error('decryption key must be 16 bytes');
  const buf = await fsp.readFile(inPath);
  const r = decryptBuffer(buf, k, opts.onProgress);
  await fsp.writeFile(outPath, buf);
  return r;
}

module.exports = { decryptFile, decryptBuffer, isEncrypted };
