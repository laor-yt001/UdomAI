'use strict';

/**
 * Hongguo / Fanqie mobile-app video API client.
 *
 * Flow (same as the official app):
 *   1. POST /novel/player/multi_video_model/v1/  (signed)  -> video_model with a `fallback_api` URL
 *   2. GET  fallback_api                                    -> video_info.data.video_list (all definitions)
 *   3. pick the best definition, decode its URL, derive the 16-byte content key from `spade_a`
 *
 * The MP4 behind the returned URL is ISO Common Encryption (cenc, AES-CTR);
 * see ./cenc.js for the decryption step.
 */

const crypto = require('crypto');
const sign = require('./iqsign');

const VIDEO_MODEL_HOSTS = [
  'https://api5-normal-sinfonlinea.fqnovel.com',
  'https://api5-normal-sinfonlineb.fqnovel.com',
];
const VIDEO_TAB_MODEL_PATH = '/reading/bookapi/video_tab/video_model/v1/';
const VIDEO_MODEL_PATH = '/novel/player/multi_video_model/v1/';
const VIDEO_MODEL_QUERY =
  'iid=%IID%&device_id=%DID%&ac=wifi&channel=update_64&aid=8662&app_name=novelread&version_code=71332&version_name=7.1.3.32&device_platform=android&os=android&ssmix=a&device_type=25053RT47C&device_brand=Redmi&language=zh&os_api=36&os_version=16&manifest_version_code=71332&resolution=1280*2772&dpi=520&update_version_code=71332&host_abi=arm64-v8a&dragon_device_type=phone&pv_player=71332&compliance_status=0&need_personal_recommend=1&player_so_load=1&is_android_pad_screen=0&codec_type=0&format_type=h264';
const TYPE_CODES = ['1004', '1001', '0', '1', '2', '19'];

/** Headers the CDN expects when the encrypted MP4 is fetched. */
const MEDIA_HEADERS = { 'User-Agent': 'com.phoenix.read/71332', Referer: 'https://novel.snssdk.com/' };

/* ------------------------------------------------------------------ */
/* device identity                                                     */
/* ------------------------------------------------------------------ */

let device = null;
function getDevice() {
  const valid = (v) => (v && /^\d{19}$/.test(String(v).trim()) ? String(v).trim() : '');
  return {
    deviceId: valid(process.env.DUANJU_DEVICE_ID) || sign.randomDecimalId(),
    installId: valid(process.env.DUANJU_INSTALL_ID) || sign.randomDecimalId(),
  };
}

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

function decodeBase64(s) {
  s = String(s).trim();
  const n = s.length % 4;
  if (n) s += '='.repeat(4 - n);
  // Buffer accepts both standard and url-safe alphabets.
  return Buffer.from(s, 'base64');
}

const SPADE_CONSTANTS = Buffer.from([
  0x4d, 0xd4, 0xc2, 0xe6, 0xb8, 0x31, 0x62, 0x09, 0x0e, 0x52, 0xb3, 0xc7, 0xa6, 0x73, 0x3b, 0xa4, 0x1c, 0xb2, 0x46, 0x2b, 0x82, 0x9a, 0xb5, 0x8a, 0x19,
  0x6b, 0x39, 0xdb, 0x57, 0x17, 0x75, 0x24, 0xf4, 0x9b, 0xaf, 0x7f, 0x08, 0xe8, 0xd6, 0x8d, 0x26, 0xa7, 0x2e, 0x37, 0xc1, 0xa9, 0x5a, 0x2f, 0x1f, 0x05,
  0xa5, 0x18, 0x92, 0xae, 0xf2, 0x94, 0x97, 0x32, 0xb6, 0x2a, 0x38, 0xaa, 0xdd, 0x58,
]);

function bitsOnes(v) {
  let n = 0;
  while (v) {
    n += v & 1;
    v >>>= 1;
  }
  return n & 0xff;
}

/** spade_a (base64) -> 16-byte content key as lowercase hex. */
function deriveContentKey(spade) {
  const raw = decodeBase64(spade);
  if (raw.length < 3) throw new Error('spade_a is too short');
  let v8 = raw.length - (raw[0] ^ raw[1] ^ raw[2]) + 47;
  if (v8 <= 0 || v8 > raw.length * 2) throw new Error(`spade_a computed length ${v8} is invalid`);
  if (1 + v8 > raw.length) v8 = raw.length - 1;
  if (v8 < 33) throw new Error('spade_a payload is too short');
  const v13 = Buffer.from(raw.subarray(1, 1 + v8));
  let va = 85;
  let vb = 246;
  for (let i = 0; i < v13.length; i++) {
    let previous;
    if (i & 1) {
      previous = va;
      va = v13[i];
    } else {
      previous = vb;
      vb = v13[i];
    }
    v13[i] = (-21 - bitsOnes(i) + (previous ^ v13[i])) & 0xff;
  }
  const hex = v13.subarray(1, 33).toString('latin1');
  if (!/^[0-9a-fA-F]{32}$/.test(hex)) throw new Error('invalid content key in spade_a');
  return hex.toLowerCase();
}

/** Encrypted (base64) media URL + key_seed -> plain URL. */
function decryptSpadeUrl(encoded, seed) {
  const raw = decodeBase64(encoded);
  if (raw.length < 5 || raw[0] !== 0xa8 || raw[2] !== 1 || raw[3] !== 0) throw new Error('invalid encrypted URL header');
  let ct = raw.subarray(4);
  ct = ct.subarray(0, Math.floor(ct.length / 16) * 16);
  if (!ct.length) throw new Error('empty encrypted URL');
  const h1 = crypto.createHash('sha512').update(seed).digest();
  const h2 = crypto.createHash('sha512').update(Buffer.concat([h1, SPADE_CONSTANTS])).digest();
  const d = crypto.createDecipheriv('aes-128-cbc', h2.subarray(0, 16), h2.subarray(16, 32));
  d.setAutoPadding(false);
  let plain = Buffer.concat([d.update(ct), d.final()]);
  const pad = plain[plain.length - 1];
  if (pad >= 1 && pad <= 16 && pad <= plain.length) plain = plain.subarray(0, plain.length - pad);
  return plain.toString('utf8').replace(/\0+$/, '');
}

function mediaValue(v) {
  if (typeof v === 'string') return v.trim();
  if (Array.isArray(v)) {
    for (const e of v) {
      const s = mediaValue(e);
      if (s) return s;
    }
    return '';
  }
  if (v && typeof v === 'object') {
    for (const k of ['url', 'uri', 'src', 'download_url', 'url_list', 'urls']) {
      const s = mediaValue(v[k]);
      if (s) return s;
    }
  }
  return '';
}
const num = (v) => {
  const n = typeof v === 'string' ? parseFloat(v) : Number(v);
  return Number.isFinite(n) ? n : 0;
};
const first = (...vals) => vals.find((v) => v !== undefined && v !== null && v !== '');

function compact(v) {
  try {
    const s = JSON.stringify(v);
    return s.length > 300 ? s.slice(0, 300) + '...' : s;
  } catch {
    return String(v);
  }
}

async function fetchJson(url, { method = 'GET', headers = {}, body, signal, retries = 2, timeoutMs = 7000 } = {}) {
  let last;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const timerCtrl = typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(timeoutMs) : null;
      let effectiveSignal = signal;
      if (timerCtrl && signal) {
        effectiveSignal = AbortSignal.any ? AbortSignal.any([signal, timerCtrl]) : signal;
      } else if (timerCtrl) {
        effectiveSignal = timerCtrl;
      }
      const res = await fetch(url, { method, headers, body, signal: effectiveSignal, redirect: 'follow' });
      const text = await res.text();
      if (res.status < 200 || res.status >= 300) {
        last = new Error(`HTTP ${res.status} from ${new URL(url).host}: ${text.slice(0, 200)}`);
      } else {
        try {
          return JSON.parse(text);
        } catch {
          last = new Error(`Invalid JSON from ${new URL(url).host}: ${text.slice(0, 200)}`);
        }
      }
    } catch (e) {
      if (e && (e.name === 'AbortError' || (signal && signal.aborted))) throw e;
      last = e;
    }
    await new Promise((r) => setTimeout(r, 200 * (attempt + 1)));
  }
  throw last;
}

/* ------------------------------------------------------------------ */
/* video model                                                         */
/* ------------------------------------------------------------------ */

function makeBody(typeCode, vid) {
  return Buffer.from(
    JSON.stringify({
      biz_param: {
        detail_page_version: 0,
        device_level: 3,
        disable_digg_stat: false,
        need_all_video_definition: true,
        need_mp4_align: false,
        use_os_player: true,
        use_server_dns: false,
        video_platform: 1024,
        format_type: 'h264',
        codec_type: 0,
      },
      mixed_video_id_map: { [typeCode]: [String(vid)] },
    })
  );
}

function parseFallback(v) {
  if (typeof v === 'string') {
    const s = v.trim();
    if (s.startsWith('{')) {
      try {
        return mediaValue(JSON.parse(s).fallback_api);
      } catch {
        return '';
      }
    }
    return s;
  }
  if (Array.isArray(v)) return v.length ? mediaValue(v[0]) : '';
  if (v && typeof v === 'object') return mediaValue(v.fallback_api);
  return '';
}

function extractModel(root, vid) {
  const data = root && root.data;
  if (!data || typeof data !== 'object') {
    const msg = root && (root.message || root.msg);
    throw new Error('response has no data' + (msg ? ` (${msg})` : '') + (root && root.code != null ? ` code=${root.code}` : ''));
  }
  let entry = data[vid];
  if (!entry && data.video_model) entry = data;
  if (!entry && root.video_model) entry = root;
  if (!entry || typeof entry !== 'object') throw new Error(`video ${vid} absent from response`);
  let model = entry.video_model;
  if (typeof model === 'string') {
    try {
      model = JSON.parse(model);
    } catch {
      model = null;
    }
  }
  if (!model || typeof model !== 'object') throw new Error('video_model is missing');
  return model;
}

/** Fetch the video model for one vid (tries the known content type codes and hosts). */
async function fetchVideoModel(vid, opts = {}) {
  const signal = opts.signal;
  const seriesId = opts.seriesId;

  // 1) Primary: Native mobile short drama API (GET /reading/bookapi/video_tab/video_model/v1/)
  for (const host of VIDEO_MODEL_HOSTS) {
    if (signal && signal.aborted) throw new Error('Aborted');
    const dev = getDevice();
    let q = `video_id=${vid}`;
    if (seriesId) q += `&series_id=${seriesId}`;
    q += '&' + VIDEO_MODEL_QUERY.replace('%IID%', dev.installId).replace('%DID%', dev.deviceId);
    const params = new sign.OrderedParams(q);
    const endpoint = `${host}${VIDEO_TAB_MODEL_PATH}`;
    const { url, headers } = sign.signRequest(endpoint, params, null, dev);
    try {
      const root = await fetchJson(url, { method: 'GET', headers, signal, retries: 0 });
      if (root && (root.code === 0 || root.data)) {
        return extractModel(root, String(vid));
      }
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
    }
  }

  // 2) Fallback: Multi-video model API (POST /novel/player/multi_video_model/v1/)
  const maxAttempts = 6;
  let lastErr = null;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    if (signal && signal.aborted) throw new Error('Aborted');
    const dev = getDevice();
    const params = new sign.OrderedParams(VIDEO_MODEL_QUERY.replace('%IID%', dev.installId).replace('%DID%', dev.deviceId));

    let hitEmpty = false;
    for (const host of VIDEO_MODEL_HOSTS) {
      const endpoint = `${host}${VIDEO_MODEL_PATH}`;
      for (const typ of TYPE_CODES) {
        if (signal && signal.aborted) throw new Error('Aborted');
        const body = makeBody(typ, vid);
        const { url, headers } = sign.signRequest(endpoint, params, body, dev);
        let root;
        try {
          root = await fetchJson(url, { method: 'POST', headers, body, signal, retries: 0 });
        } catch (e) {
          if (e && e.name === 'AbortError') throw e;
          lastErr = e;
          if (String(e.message || '').includes('Invalid JSON')) {
            hitEmpty = true;
            continue;
          }
          continue;
        }
        try {
          return extractModel(root, String(vid));
        } catch (e) {
          lastErr = new Error(`type ${typ}: ${e.message} (response=${compact(root)})`);
        }
      }
    }

    if (attempt < maxAttempts) {
      await new Promise((r) => setTimeout(r, hitEmpty ? 500 : 350));
    }
  }
  throw new Error('app video API failed: ' + (lastErr ? lastErr.message : 'unknown error'));
}

function variantsOf(videoData) {
  const list = videoData && videoData.video_list;
  const out = [];
  if (Array.isArray(list)) {
    for (const item of list) if (item && typeof item === 'object') out.push(item);
  } else if (list && typeof list === 'object') {
    for (const k of Object.keys(list)) if (list[k] && typeof list[k] === 'object') out.push(list[k]);
  }
  return out;
}

function pickVariant(variants, maxHeight) {
  function codecScore(item) {
    const c = String(item.codec_type || item.vtype || item.codec || '').toLowerCase();
    const def = String(item.definition || item.quality || '').toLowerCase();
    const url = String(first(item.main_url, item.play_addr, item.backup_url_1, item.url) || '').toLowerCase();
    // Strictly penalize ByteVC2 / bvc2 which standard players and ffmpeg cannot decode (causes audio-only, black screen)
    if (c.includes('bvc2') || c.includes('bytevc2') || def.includes('bvc2') || url.includes('bvc2') || url.includes('bytevc2')) {
      return -100;
    }
    // H.264 / AVC / MP4 is standard and universally supported everywhere (Telegram, Mobile, Web)
    if (c.includes('h264') || c.includes('avc') || c.includes('mp4') || c === '0' || url.includes('codec_type=h264') || url.includes('codec_type=0') || url.includes('h264')) {
      return 200;
    }
    // ByteVC1 / HEVC
    if (c.includes('bytevc1') || c.includes('hevc') || c.includes('h265') || c === '1' || url.includes('codec_type=1')) {
      return 50;
    }
    return 10;
  }

  const scored = variants
    .map((item) => ({
      item,
      h: num(first(item.vheight, item.height)),
      w: num(first(item.vwidth, item.width)),
      br: num(item.bitrate),
      cs: codecScore(item),
    }))
    .filter((v) => mediaValue(first(v.item.main_url, v.item.play_addr, v.item.backup_url_1, v.item.url)));
  if (!scored.length) return null;

  // 1. Universally playable codec (H.264 > HEVC > unplayable bvc2)
  // 2. Resolution (height)
  // 3. Bitrate
  scored.sort((a, b) => {
    if (a.cs !== b.cs) return b.cs - a.cs;
    if (a.h !== b.h) return b.h - a.h;
    return b.br - a.br;
  });

  if (maxHeight) {
    const ok = scored.filter((v) => v.h <= maxHeight);
    if (ok.length) return ok[0];
  }
  return scored[0];
}

/**
 * Resolve a fresh (time-limited) media URL + content key for one vid.
 * @param {string} vid
 * @param {{signal?:AbortSignal, maxHeight?:number}} [opts]
 * @returns {Promise<{mainUrl:string,key:string|null,width:number,height:number,bitrate:number,
 *   size:number,duration:number,definition:string,headers:Object,variants:Array}>}
 */
async function resolveVideo(vid, opts = {}) {
  const model = await fetchVideoModel(vid, opts);
  let videoData = null;
  const fallback = parseFallback(model.fallback_api);
  if (fallback) {
    // 1. First probe codec_type=0 (H.264/AVC) which is standard, zero black-screen risk, viewable everywhere!
    let probeUrl0 = fallback.includes('codec_type=') ? fallback.replace(/codec_type=\d+/, 'codec_type=0') : (fallback + '&codec_type=0');
    try {
      const root0 = await fetchJson(probeUrl0, { headers: { 'User-Agent': sign.USER_AGENT }, signal: opts.signal });
      const vd0 = (root0 && root0.video_info && root0.video_info.data) || (root0 && root0.data && root0.data.video_list ? root0.data : null);
      if (vd0 && variantsOf(vd0).length) {
        const checkVars = variantsOf(vd0);
        const hasH264 = checkVars.some(v => {
          const c = String(v.codec_type || v.vtype || '').toLowerCase();
          const u = String(first(v.main_url, v.play_addr, v.backup_url_1, v.url) || '').toLowerCase();
          return c.includes('h264') || c.includes('avc') || c === '0' || u.includes('codec_type=0') || (!c.includes('bvc') && !c.includes('bytevc'));
        });
        if (hasH264) {
          videoData = vd0;
        }
      }
    } catch { }

    // 2. If no H.264, probe codec_type=1 (HEVC/ByteVC1)
    if (!videoData) {
      let probeUrl1 = fallback.includes('codec_type=') ? fallback.replace(/codec_type=\d+/, 'codec_type=1') : (fallback + '&codec_type=1');
      try {
        const root1 = await fetchJson(probeUrl1, { headers: { 'User-Agent': sign.USER_AGENT }, signal: opts.signal });
        const vd1 = (root1 && root1.video_info && root1.video_info.data) || (root1 && root1.data && root1.data.video_list ? root1.data : null);
        if (vd1 && variantsOf(vd1).length) {
          const checkVars = variantsOf(vd1);
          const hasPlayable = checkVars.some(v => {
            const c = String(v.codec_type || v.vtype || '').toLowerCase();
            return !c.includes('bvc2') && !c.includes('bytevc2');
          });
          if (hasPlayable) {
            videoData = vd1;
          }
        }
      } catch { }
    }

    // 3. Fallback to default API response if neither probe worked
    if (!videoData) {
      const root = await fetchJson(fallback, { headers: { 'User-Agent': sign.USER_AGENT }, signal: opts.signal });
      videoData = root && root.video_info && root.video_info.data;
      if (!videoData && root && root.data && root.data.video_list) videoData = root.data;
    }
  }
  if ((!videoData || !variantsOf(videoData).length) && variantsOf(model).length) videoData = model;
  if (!videoData) throw new Error('video API returned no video data' + (fallback ? '' : ' (no fallback_api in video_model)'));

  const variants = variantsOf(videoData);
  const best = pickVariant(variants, opts.maxHeight);
  if (!best) throw new Error('video API returned no playable formats (response=' + compact(videoData) + ')');
  const item = best.item;
  let rawUrl = mediaValue(first(item.main_url, item.play_addr, item.backup_url_1, item.url));
  const seedText = videoData.key_seed || model.key_seed;
  if (seedText && !/^https?:\/\//i.test(rawUrl)) {
    try {
      const decoded = decryptSpadeUrl(rawUrl, decodeBase64(String(seedText)));
      if (/^https?:\/\//i.test(decoded)) rawUrl = decoded;
    } catch {
      /* not an encrypted URL */
    }
  }
  if (rawUrl.startsWith('//')) rawUrl = 'https:' + rawUrl;
  if (!/^https?:\/\//i.test(rawUrl)) throw new Error('selected format has no usable URL');

  let key = null;
  if (item.spade_a) key = deriveContentKey(String(item.spade_a));
  else if (item.encrypt === true || item.encrypt === 1 || item.encrypt === 'true') {
    throw new Error('format is encrypted but carries no spade_a key');
  }

  return {
    mainUrl: rawUrl,
    key,
    width: best.w || undefined,
    height: best.h || undefined,
    bitrate: best.br || undefined,
    size: num(first(item.size, item.data_size, item.file_size)) || undefined,
    duration: num(first(model.duration, model.video_duration, videoData.duration, videoData.video_duration)) || undefined,
    definition: String(first(item.definition, item.quality, best.h ? best.h + 'p' : '') || ''),
    codec: String(first(item.codec_type, item.codec, '') || ''),
    headers: Object.assign({}, MEDIA_HEADERS),
    variants: variants.map((v) => ({
      definition: String(first(v.definition, v.quality, '') || ''),
      height: num(first(v.vheight, v.height)),
      width: num(first(v.vwidth, v.width)),
      bitrate: num(v.bitrate),
      size: num(first(v.size, v.data_size, v.file_size)),
      encrypted: !!v.spade_a,
    })),
  };
}

module.exports = {
  resolveVideo,
  fetchVideoModel,
  deriveContentKey,
  decryptSpadeUrl,
  getDevice,
  MEDIA_HEADERS,
};
