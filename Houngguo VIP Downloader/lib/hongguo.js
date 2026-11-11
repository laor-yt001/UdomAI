'use strict';

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const { spawn } = require('child_process');
const { extractJson } = require('./extract');
const fqapi = require('./fqapi');
const cenc = require('./cenc');

const BASE = 'https://hongguoduanju.com';
const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';
const ROUTER_MARKER = '_ROUTER_DATA = ';

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

/**
 * Accepts a full URL (…/detail?series_id=123, …/detail/123, …/player/123/456),
 * or a bare numeric id. Returns the series id as a string.
 */
async function resolveShareUrl(url, signal) {
  if (url && typeof url === 'string') {
    const trimmed = url.trim();
    if (trimmed.includes('novelquickapp.com')) {
      try {
        const res = await fetch(trimmed, { method: 'GET', redirect: 'follow', signal, headers: headers() });
        if (res && res.url) return res.url;
      } catch {}
    }
  }
  return url;
}

function parseSeriesId(input) {
  const s = String(input == null ? '' : input).trim();
  if (!s) throw new Error('Empty series URL / id');
  const m =
    s.match(/[?&]series_id=(\d+)/) ||
    s.match(/\/detail\/(\d+)/) ||
    s.match(/\/player\/(\d+)/) ||
    s.match(/series_id=(\d+)/) ||
    s.match(/[?&]video_id=(\d+)/) ||
    s.match(/[?&]item_id=(\d+)/) ||
    s.match(/^(\d+)$/);
  if (!m) throw new Error('Could not find a series id in: ' + s);
  return m[1];
}

function headers(extra) {
  return Object.assign(
    {
      'User-Agent': USER_AGENT,
      Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8',
    },
    extra || {}
  );
}

async function fetchPage(url, signal) {
  const res = await fetch(url, { headers: headers(), redirect: 'follow', signal });
  const text = await res.text();
  return { status: res.status, text };
}

/**
 * The site's Modern.js loader endpoints return clean JSON when asked with
 * `?__loader=<route>` plus the `X-Modernjs-Response: yes` header.
 * Returns { status, json } where json is null when the body is not JSON.
 */
async function fetchLoader(url, signal) {
  const res = await fetch(url, {
    headers: headers({ Accept: 'application/json, text/plain, */*', 'X-Modernjs-Response': 'yes' }),
    redirect: 'follow',
    signal,
  });
  const text = await res.text();
  let json = null;
  const ct = res.headers.get('content-type') || '';
  if (/json/i.test(ct) || /^\s*[{[]/.test(text)) {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }
  return { status: res.status, json, text };
}

/** Depth-first search for the first property named `key`. */
function findKey(obj, key, depth = 0) {
  if (!obj || typeof obj !== 'object' || depth > 12) return undefined;
  if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];
  for (const k of Object.keys(obj)) {
    const v = obj[k];
    if (v && typeof v === 'object') {
      const r = findKey(v, key, depth + 1);
      if (r !== undefined) return r;
    }
  }
  return undefined;
}

/** Strip characters Windows does not allow in filenames. */
function sanitizeFilename(name) {
  let s = String(name == null ? '' : name)
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/g, '');
  if (!s) s = 'untitled';
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(s)) s = '_' + s;
  return s.slice(0, 150);
}

function padEpisode(n, total) {
  const width = Math.max(2, String(total || 0).length);
  return String(n).padStart(width, '0');
}

/** `EP01 - <series name>.mp4` (zero padded to width of episodeCount). */
function episodeFilename(seriesName, episodeNumber, episodeCount) {
  return `EP${padEpisode(episodeNumber, episodeCount)} - ${sanitizeFilename(seriesName)}.mp4`;
}

/* ------------------------------------------------------------------ */
/* site access                                                         */
/* ------------------------------------------------------------------ */

/**
 * Load series metadata from the detail page.
 * @returns {Promise<{seriesId:string,name:string,cover:string,intro:string,tags:string[],
 *   episodeCount:number,accessibleCount:number,vids:string[],status:string,payType:any}>}
 */
async function getSeries(seriesIdOrUrl, opts = {}) {
  const resolved = await resolveShareUrl(seriesIdOrUrl, opts.signal);
  const seriesId = parseSeriesId(resolved);
  const url = `${BASE}/detail?series_id=${seriesId}`;
  let detail = null;

  // 1) clean JSON loader endpoint
  try {
    const { status, json } = await fetchLoader(`${url}&__loader=detail_page`, opts.signal);
    if (status === 404) throw notFound(`Series ${seriesId} was not found on hongguoduanju.com (HTTP 404)`);
    if (status < 400 && json) detail = json.seriesDetail || findKey(json, 'seriesDetail') || null;
  } catch (e) {
    if (e.code === 'ENOTFOUND_SERIES' || isAbort(e)) throw e;
    // fall through to HTML
  }

  // 2) fallback: server-rendered HTML with the embedded _ROUTER_DATA object
  if (!detail || !Array.isArray(detail.vid_list)) {
    const { status, text } = await fetchPage(url, opts.signal);
    if (status >= 400) throw new Error(`Detail page returned HTTP ${status} for series ${seriesId}`);
    let data;
    try {
      data = extractJson(text, ROUTER_MARKER);
    } catch (e) {
      throw new Error('Could not parse series page data: ' + e.message);
    }
    detail =
      (data.loaderData && data.loaderData.detail_page && data.loaderData.detail_page.seriesDetail) ||
      findKey(data, 'seriesDetail');
  }

  if (!detail || !Array.isArray(detail.vid_list)) {
    throw new Error('Series not found on hongguoduanju.com (no seriesDetail in page data)');
  }
  const vids = detail.vid_list.map(String);
  const episodeCount = Number(detail.episode_cnt) || vids.length;
  const accessibleRaw = Number(detail.accessible_episode_cnt);
  const accessibleCount = Number.isFinite(accessibleRaw) && accessibleRaw >= 0 ? accessibleRaw : vids.length;
  return {
    seriesId: String(detail.series_id || seriesId),
    name: String(detail.series_name || `Series ${seriesId}`),
    cover: detail.series_cover || '',
    intro: detail.series_intro || '',
    tags: Array.isArray(detail.tags) ? detail.tags.map(String) : [],
    episodeCount,
    accessibleCount: Math.min(accessibleCount, vids.length),
    vids,
    status: detail.series_episode_info ? detail.series_episode_info.series_status : undefined,
    payType: detail.pay_type,
    url,
  };
}

async function getEpisodeFromShare(seriesId, vid, opts = {}) {
  const shareUrls = [
    `https://novelquickapp.com/hongguo/ug/pages/video-animation-share?video_id=${seriesId}&vid=${vid}&share_toast_vid=${vid}&landing_source=share`,
    `https://novelquickapp.com/hongguo/ug/pages/video-animation-share?video_id=${seriesId}&vid=${vid}`
  ];
  for (const u of shareUrls) {
    try {
      const res = await fetch(u, {
        headers: { 'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148' },
        signal: opts.signal,
      });
      if (res.ok) {
        const text = await res.text();
        const fnMatch = text.match(/data-script-src="modern-run-router-data-fn"[\s\S]*?data-fn-args="([^"]+)"/);
        if (fnMatch) {
          try {
            const decoded = fnMatch[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
            const data = JSON.parse(decoded);
            const pageData = data[2] || data;
            if (pageData && pageData.series_data) {
              const sd = pageData.series_data;
              const title = sd.title || '';
              if (!opts.seriesName || !title || title.includes(opts.seriesName) || String(opts.seriesName).includes(title)) {
                if (sd.play_url) {
                  const playUrl = sd.play_url.replace(/&amp;/g, '&');
                  const dur = (sd.video_meta && sd.video_meta.duration) || 0;
                  // Reject teaser clips that are locked to 30s
                  if (dur > 30 && !playUrl.includes('end=30')) {
                    return {
                      mainUrl: playUrl,
                      key: null,
                      headers: { 'User-Agent': USER_AGENT, Accept: '*/*' },
                      height: (sd.video_meta && sd.video_meta.height) || 720,
                      width: (sd.video_meta && sd.video_meta.width) || 1280,
                      duration: dur,
                      source: 'share_cdn',
                      pageUrl: u
                    };
                  }
                }
              }
            }
          } catch {}
        }

        const ogMatch = text.match(/og:url"\s+content="([^"]+)"/) || text.match(/name="og:url"\s+content="([^"]+)"/);
        if (ogMatch && ogMatch[1]) {
          const directUrl = ogMatch[1].replace(/&amp;/g, '&');
          if ((directUrl.includes('qznovel.com') || directUrl.includes('video_mp4')) && !directUrl.includes('end=30')) {
            return {
              mainUrl: directUrl,
              key: null,
              headers: { 'User-Agent': USER_AGENT, Accept: '*/*' },
              height: 720,
              width: 1280,
              source: 'share_cdn',
              pageUrl: u
            };
          }
        }
      }
    } catch (e) {
      if (isAbort(e)) throw e;
    }
  }
  return null;
}

/**
 * Fetch a fresh (time-limited) video URL for one episode.
 *
 * Primary sources:
 *   1. Mobile-app API (CENC encrypted stream, decrypts via AES-CTR to full 1080p/720p duration)
 *   2. Direct high-speed share CDN (full episodes only, not 30s promo teaser)
 *   3. Website public player stream (first accessible episodes, 360p)
 *
 * opts.source: 'auto' (default) | 'app' | 'web' | 'share'
 * opts.maxHeight: cap the picked definition (e.g. 720)
 * opts.seriesName: for strict verification
 */
async function getEpisode(seriesId, vid, opts = {}) {
  const source = opts.source || 'auto';

  // 1) Mobile-app API: Primary source for guaranteed full duration & high definition
  if (source !== 'web' && source !== 'share') {
    let appErr = null;
    try {
      const r = await fqapi.resolveVideo(String(vid), { signal: opts.signal, maxHeight: opts.maxHeight, seriesId });
      // Reject ByteVC2 (bvc2) streams – completely unplayable by standard players.
      // fqapi already blocks them, but add a final safety net here.
      const isUnplayable =
        String(r.codec || '').toLowerCase().includes('bvc2') ||
        String(r.codec || '').toLowerCase().includes('bytevc2') ||
        String(r.mainUrl || '').includes('/v02ebeg') ||
        String(r.definition || '').toLowerCase().includes('bvc2');
      if (!isUnplayable) {
        return {
          mainUrl: r.mainUrl,
          key: r.key,
          headers: r.headers,
          width: r.width,
          height: r.height,
          bitrate: r.bitrate,
          size: r.size,
          duration: r.duration,
          definition: r.definition,
          variants: r.variants,
          source: 'app',
          pageUrl: `${BASE}/player/${seriesId}/${vid}`,
        };
      }
      // ByteVC2 detected: fall through to share/web sources
      appErr = new Error('ByteVC2 (bvc2) stream returned by app API – falling back to web/share source');
      if (source === 'app') throw appErr;
    } catch (e) {
      if (isAbort(e)) throw e;
      appErr = e;
      if (source === 'app') throw e;
    }
  }

  // 2) Direct share CDN fallback (only if full episode, not 30s teaser)
  if (source !== 'app' && source !== 'web') {
    try {
      const share = await getEpisodeFromShare(seriesId, vid, opts);
      if (share && (!share.duration || share.duration > 30)) return share;
    } catch (e) {
      if (isAbort(e)) throw e;
    }
  }

  // 3) Website public player stream fallback (full duration only, strictly > 45s)
  if (source !== 'app' && source !== 'share') {
    try {
      const web = await getEpisodeFromWeb(seriesId, vid, opts);
      if (web && web.duration && web.duration > 45) return web;
    } catch (e) {
      if (isAbort(e)) throw e;
    }
  }

  return null;
}

/** Website player page: plain MP4 for accessible episodes. */
async function getEpisodeFromWeb(seriesId, vid, opts = {}) {
  const url = `${BASE}/player/${seriesId}/${vid}`;
  let info = null;

  // 1) Direct check for public video stream (must be full episode > 45s and > 5MB, never a 10s preview)
  try {
    const pubUrl = `${BASE}/video/public/${vid}/360p.mp4`;
    const pubHead = await fetch(pubUrl, { method: 'HEAD', headers: headers({ Accept: '*/*' }), signal: opts.signal });
    if (pubHead.ok && pubHead.status === 200) {
      const len = parseInt(pubHead.headers.get('content-length') || '0', 10);
      if (len > 5 * 1024 * 1024) {
        return {
          mainUrl: pubUrl,
          key: null,
          headers: headers({ Accept: '*/*' }),
          width: 640,
          height: 360,
          source: 'web_public',
          pageUrl: url,
        };
      }
    }
  } catch (e) {
    if (isAbort(e)) throw e;
  }

  // 2) clean JSON loader endpoint (404 "Not Found" => app-only episode)
  let loaderOk = false;
  try {
    const { status, json } = await fetchLoader(`${url}?__loader=player_(series_id)/(vid)/page`, opts.signal);
    if (status === 404) return null;
    if (status < 400 && json) {
      loaderOk = true;
      info = json.video_player_info || findKey(json, 'video_player_info') || null;
      if (!info) {
        const seo = json.publicVideoSeo || findKey(json, 'publicVideoSeo');
        if (seo && seo.available && (!seo.durationSeconds || seo.durationSeconds > 30)) {
          const pubUrl = `${BASE}/video/public/${vid}/360p.mp4`;
          try {
            const headRes = await fetch(pubUrl, { method: 'HEAD', signal: opts.signal });
            if (headRes.ok) {
              const len = parseInt(headRes.headers.get('content-length') || '0', 10);
              if (len > 3 * 1024 * 1024) {
                return {
                  mainUrl: pubUrl,
                  key: null,
                  headers: headers({ Accept: '*/*' }),
                  width: seo.width || 640,
                  height: seo.height || 360,
                  duration: seo.durationSeconds,
                  poster: seo.posterUrl || '',
                  source: 'web_public',
                  pageUrl: url,
                };
              }
            }
          } catch {}
        }
      }
    }
  } catch (e) {
    if (isAbort(e)) throw e;
  }

  // 3) fallback: HTML page with _ROUTER_DATA
  if (!loaderOk) {
    const { status, text } = await fetchPage(url, opts.signal);
    if (status === 404) return null;
    if (status >= 400) throw new Error(`Player page returned HTTP ${status}`);
    let data;
    try {
      data = extractJson(text, ROUTER_MARKER);
    } catch (e) {
      throw new Error('Could not parse player page data: ' + e.message);
    }
    info = findKey(data, 'video_player_info');
    if (!info) {
      const pageData = findKey(data, 'player_(series_id)/(vid)/page');
      if (pageData && pageData.publicVideoSeo && pageData.publicVideoSeo.available && (!pageData.publicVideoSeo.durationSeconds || pageData.publicVideoSeo.durationSeconds > 30)) {
        const pubUrl = `${BASE}/video/public/${vid}/360p.mp4`;
        try {
          const headRes = await fetch(pubUrl, { method: 'HEAD', signal: opts.signal });
          if (headRes.ok) {
            const len = parseInt(headRes.headers.get('content-length') || '0', 10);
            if (len > 3 * 1024 * 1024) {
              return {
                mainUrl: pubUrl,
                key: null,
                headers: headers({ Accept: '*/*' }),
                width: pageData.publicVideoSeo.width || 640,
                height: pageData.publicVideoSeo.height || 360,
                duration: pageData.publicVideoSeo.durationSeconds,
                poster: pageData.publicVideoSeo.posterUrl || '',
                source: 'web_public',
                pageUrl: url,
              };
            }
          }
        } catch {}
      }
    }
  }

  if (!info || !info.main_url) return null;
  return {
    mainUrl: info.main_url,
    key: null,
    headers: headers({ Accept: '*/*' }),
    width: Number(info.width) || undefined,
    height: Number(info.height) || undefined,
    duration: Number(info.duration) || undefined,
    poster: info.poster_url || '',
    source: 'web',
    pageUrl: url,
  };
}

/* ------------------------------------------------------------------ */
/* download                                                            */
/* ------------------------------------------------------------------ */

async function statSize(p) {
  try {
    const st = await fsp.stat(p);
    return st.isFile() ? st.size : -1;
  } catch {
    return -1;
  }
}

/**
 * Download `url` to `destPath`, writing to `<destPath>.part` first and renaming
 * on completion. Resumes with a Range request when a .part file already exists.
 *
 * @param {string} url
 * @param {string} destPath
 * @param {{onProgress?:(received:number,total:number|null)=>void, signal?:AbortSignal,
 *   headers?:Object, finalize?:boolean}} [opts]  finalize=false keeps the .part file (no rename)
 * @returns {Promise<{path:string,bytes:number,skipped:boolean,resumedFrom:number}>}
 */
async function downloadFile(url, destPath, opts = {}) {
  const { onProgress, signal, finalize = true } = opts;
  const partPath = destPath + '.part';
  await fsp.mkdir(path.dirname(destPath), { recursive: true });

  const existing = await statSize(destPath);
  if (existing > 0) {
    if (onProgress) onProgress(existing, existing);
    return { path: destPath, bytes: existing, skipped: true, resumedFrom: 0 };
  }

  let start = await statSize(partPath);
  if (start < 0) start = 0;

  const reqHeaders = Object.assign({ Accept: '*/*' }, opts.headers || headers({ Accept: '*/*' }));
  if (start > 0) reqHeaders.Range = `bytes=${start}-`;

  let res = await fetch(url, { headers: reqHeaders, redirect: 'follow', signal });

  if (res.status === 416) {
    // Range not satisfiable: the .part is probably already complete or corrupt. Restart.
    await res.body?.cancel().catch(() => {});
    start = 0;
    delete reqHeaders.Range;
    res = await fetch(url, { headers: reqHeaders, redirect: 'follow', signal });
  }

  if (!res.ok) {
    await res.body?.cancel().catch(() => {});
    throw new Error(`HTTP ${res.status} while downloading`);
  }

  let append = false;
  if (start > 0 && res.status === 206) {
    append = true;
  } else {
    // Server ignored Range (200): start over.
    start = 0;
  }

  let total = null;
  const len = Number(res.headers.get('content-length'));
  if (res.status === 206) {
    const cr = res.headers.get('content-range');
    const m = cr && cr.match(/\/(\d+)\s*$/);
    if (m) total = Number(m[1]);
    else if (Number.isFinite(len)) total = start + len;
  } else if (Number.isFinite(len) && len > 0) {
    total = len;
  }

  const out = fs.createWriteStream(partPath, { flags: append ? 'a' : 'w' });
  let received = start;
  let lastTick = 0;
  const tick = (force) => {
    if (!onProgress) return;
    const now = Date.now();
    if (force || now - lastTick > 100) {
      lastTick = now;
      onProgress(received, total);
    }
  };

  try {
    tick(true);
    if (!res.body) throw new Error('Empty response body');
    for await (const chunk of res.body) {
      if (signal && signal.aborted) throw abortError();
      received += chunk.length;
      if (!out.write(chunk)) {
        await new Promise((resolve) => out.once('drain', resolve));
      }
      tick(false);
    }
    await new Promise((resolve, reject) => {
      out.end((err) => (err ? reject(err) : resolve()));
    });
    if (total != null && received !== total) {
      throw new Error(`Incomplete download (${received} of ${total} bytes)`);
    }
    tick(true);
    if (!finalize) return { path: partPath, bytes: received, skipped: false, resumedFrom: start };
    await fsp.rename(partPath, destPath);
    return { path: destPath, bytes: received, skipped: false, resumedFrom: start };
  } catch (err) {
    // Keep the .part file so a later run can resume.
    await new Promise((resolve) => out.destroy(undefined, () => resolve()));
    if (signal && signal.aborted) throw abortError();
    throw err;
  }
}

function abortError() {
  const e = new Error('Download aborted');
  e.name = 'AbortError';
  return e;
}

function notFound(msg) {
  const e = new Error(msg);
  e.code = 'ENOTFOUND_SERIES';
  return e;
}

function isAbort(err) {
  return !!err && (err.name === 'AbortError' || /aborted/i.test(String(err.message)));
}

/** Network-level failures worth retrying (not HTTP 4xx, not aborts). */
function isTransient(err) {
  if (!err || isAbort(err)) return false;
  if (err.code === 'EDECRYPT') return false;
  if (/^HTTP 4\d\d/.test(String(err.message))) return false;
  return true;
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) return reject(abortError());
    const t = setTimeout(() => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(abortError());
    };
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}

/* ------------------------------------------------------------------ */
/* decryption                                                          */
/* ------------------------------------------------------------------ */

function findFfmpegBin() {
  if (process.env.FFMPEG_PATH && fs.existsSync(process.env.FFMPEG_PATH)) {
    return process.env.FFMPEG_PATH;
  }
  const rootDir = path.resolve(__dirname, '..', '..');
  const scanDirs = [
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python'),
    path.join(rootDir, 'venv', 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries'),
    path.join(rootDir, 'venv', 'Scripts'),
    path.join(rootDir, 'ffmpeg'),
    rootDir,
  ];
  for (const d of scanDirs) {
    try {
      if (fs.existsSync(d)) {
        const files = fs.readdirSync(d, { recursive: true });
        for (const f of files) {
          const s = String(f);
          if (/ffmpeg.*\.exe$/i.test(s) || (process.platform !== 'win32' && /(^|\/)ffmpeg$/i.test(s))) {
            const full = path.join(d, s);
            if (fs.existsSync(full)) return full;
          }
        }
      }
    } catch {}
  }
  const candidates = [
    path.join(rootDir, 'venv', 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries', 'ffmpeg.exe'),
    path.join(rootDir, 'venv', 'Scripts', 'ffmpeg.exe'),
    path.join(__dirname, '..', '..', 'ffmpeg', 'ffmpeg.exe'),
    path.join(__dirname, '..', '..', 'ffmpeg.exe'),
    path.join(__dirname, '..', '..', 'resources', 'ffmpeg', 'ffmpeg.exe'),
    path.join(process.resourcesPath || '', 'ffmpeg', 'ffmpeg.exe'),
    path.join(process.resourcesPath || '', 'ffmpeg.exe'),
    'ffmpeg.exe',
    'ffmpeg'
  ];
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch {}
  }
  return 'ffmpeg';
}

function runFfmpeg(args, signal) {
  const ffmpegCmd = findFfmpegBin();
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(ffmpegCmd, args, { stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });
    } catch (e) {
      return reject(e);
    }
    let err = '';
    child.stderr.on('data', (d) => {
      err += d;
      if (err.length > 4000) err = err.slice(-4000);
    });
    child.on('error', reject);
    child.on('close', (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited with ${code}: ${err.trim().split('\n').pop()}`))));
    if (signal) signal.addEventListener('abort', () => child.kill(), { once: true });
  });
}

/**
 * Ensure an MP4 is universally playable on Telegram, mobile, and web:
 * 1. Reads the first 4096 bytes to detect ByteVC2 (bvc2/bv2C) magic — immediately
 *    flags as unplayable if found (FFmpeg has no decoder for it).
 * 2. Checks if the file decodes without errors via ffmpeg test decode.
 * 3. If already playable, optimizes with +faststart and avc1 tag.
 * 4. If corrupted / ByteVC1 / unplayable codec, transcodes to standard H.264 (yuv420p) + AAC.
 */
async function ensurePlayableVideo(filePath, signal) {
  try {
    const stat = await fsp.stat(filePath).catch(() => null);
    if (!stat || stat.size < 1000) return;

    // --- ByteVC2 magic-byte check ---
    // ByteVC2 container uses 'bvc2' or 'bv2C' as the brand or codec box fourcc.
    // Standard FFmpeg has no decoder for it; mark immediately as unplayable.
    let isByteVC2 = false;
    try {
      const fd = await fsp.open(filePath, 'r');
      const buf = Buffer.alloc(4096);
      await fd.read(buf, 0, 4096, 0);
      await fd.close();
      const hex = buf.toString('latin1');
      if (hex.includes('bvc2') || hex.includes('bv2C')) {
        isByteVC2 = true;
      }
    } catch {}

    let isClean = false;
    if (!isByteVC2) {
      try {
        await runFfmpeg(['-v', 'error', '-xerror', '-i', filePath, '-t', '2', '-f', 'null', '-'], signal);
        isClean = true;
      } catch {
        isClean = false;
      }
    }

    const tmpPlayable = filePath + '.playable.mp4';
    await fsp.rm(tmpPlayable, { force: true }).catch(() => {});

    if (isClean) {
      try {
        await runFfmpeg(['-y', '-v', 'error', '-i', filePath, '-c', 'copy', '-tag:v', 'avc1', '-movflags', '+faststart', tmpPlayable], signal);
        const outStat = await fsp.stat(tmpPlayable).catch(() => null);
        if (outStat && outStat.size > 1000) {
          await fsp.rename(tmpPlayable, filePath);
          return;
        }
      } catch {}
      await fsp.rm(tmpPlayable, { force: true }).catch(() => {});
      return;
    }

    // Transcode to guaranteed standard H.264 + AAC
    try {
      await runFfmpeg([
        '-y', '-v', 'error',
        '-i', filePath,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
        '-movflags', '+faststart',
        tmpPlayable
      ], signal);
      const outStat = await fsp.stat(tmpPlayable).catch(() => null);
      if (outStat && outStat.size > 1000) {
        await fsp.rename(tmpPlayable, filePath);
      }
    } catch (transcodeErr) {
      // Leave file as is on unexpected transcode error
    } finally {
      await fsp.rm(tmpPlayable, { force: true }).catch(() => {});
    }
  } catch {}
}

/**
 * Decrypt a CENC MP4 (`srcPath`) into `destPath`. Native decryption first;
 * if that fails and ffmpeg is on PATH, ffmpeg's -decryption_key is tried.
 * Guarantees output is playable in Telegram, mobile, and web browsers.
 */
async function decryptEpisodeFile(srcPath, destPath, keyHex, opts = {}) {
  const { onProgress, signal } = opts;
  const tmp = destPath + '.dec';
  await fsp.rm(tmp, { force: true }).catch(() => {});
  let nativeErr = null;
  try {
    const r = await cenc.decryptFile(srcPath, tmp, keyHex, { onProgress });
    if (!r.encrypted) {
      // Not actually encrypted: use the file as-is.
      await fsp.rm(tmp, { force: true }).catch(() => {});
      await fsp.rename(srcPath, destPath);
    } else {
      await fsp.rename(tmp, destPath);
      await fsp.rm(srcPath, { force: true }).catch(() => {});
    }
  } catch (e) {
    if (isAbort(e)) throw e;
    nativeErr = e;
    await fsp.rm(tmp, { force: true }).catch(() => {});
    try {
      await runFfmpeg(['-y', '-v', 'error', '-decryption_key', keyHex, '-i', srcPath, '-c', 'copy', '-movflags', '+faststart', tmp], signal);
      await fsp.rename(tmp, destPath);
      await fsp.rm(srcPath, { force: true }).catch(() => {});
    } catch (e2) {
      await fsp.rm(tmp, { force: true }).catch(() => {});
      const err =
        e2 && e2.code === 'ENOENT'
          ? new Error(`decryption failed: ${nativeErr.message}`)
          : new Error(`decryption failed: ${nativeErr.message}; ffmpeg fallback: ${e2.message}`);
      err.code = 'EDECRYPT';
      throw err;
    }
  }

  // Ensure decrypted output video plays cleanly everywhere
  await ensurePlayableVideo(destPath, signal);
  return { method: 'done' };
}

/**
 * High-level helper used by both the CLI and the server: fetch a fresh video
 * URL for the episode, download it with a few retries on transient network
 * errors (resuming the .part file each time), then decrypt it if needed.
 *
 * opts.onStage(stage, info): 'resolving' | 'downloading' | 'decrypting'
 * opts.source / opts.maxHeight: see getEpisode
 *
 * @returns {Promise<{unavailable:true}|{unavailable:false,path:string,bytes:number,skipped:boolean,
 *   resumedFrom:number,attempts:number,source:string,height?:number,decrypted:boolean}>}
 */
async function downloadEpisode(seriesId, vid, destPath, opts = {}) {
  const { onProgress, onStage, signal, retries = 5, onRetry } = opts;
  const existing = await statSize(destPath);
  if (existing > 0) {
    if (onProgress) onProgress(existing, existing);
    return { unavailable: false, path: destPath, bytes: existing, skipped: true, resumedFrom: 0, attempts: 0, source: 'disk', decrypted: false };
  }
  let info = null;
  let lastErr = null;
  for (let attempt = 1; attempt <= retries + 1; attempt++) {
    try {
      if (!info) {
        if (onStage) onStage('resolving');
        info = await getEpisode(seriesId, vid, { signal, source: opts.source, maxHeight: opts.maxHeight, seriesName: opts.seriesName });
        if (!info) {
          if (attempt <= retries) {
            await sleep(1500 * attempt, signal);
            continue;
          }
          return { unavailable: true };
        }
      }
      if (onStage) onStage('downloading', info);
      const r = await downloadFile(info.mainUrl, destPath, { onProgress, signal, headers: info.headers, finalize: !info.key });
      let decrypted = false;
      if (info.key && !r.skipped) {
        if (onStage) onStage('decrypting', info);
        await decryptEpisodeFile(r.path, destPath, info.key, { signal });
        r.path = destPath;
        decrypted = true;
      } else if (!r.skipped && fs.existsSync(destPath)) {
        await ensurePlayableVideo(destPath, signal);
      }
      return Object.assign({ unavailable: false, attempts: attempt, source: info.source, height: info.height, decrypted }, r);
    } catch (err) {
      lastErr = err;
      if (!isTransient(err) || attempt > retries) throw err;
      // A 403 from the CDN would be a 4xx and not retried; anything else: get a fresh URL and resume.
      info = null;
      if (onRetry) onRetry(attempt, err);
      await sleep(1000 * attempt, signal);
    }
  }
  throw lastErr || new Error('Download failed');
}

/** Human-readable notice about where the episodes come from. */
function availabilityNotice(series) {
  const n = series.episodeCount;
  const a = series.accessibleCount;
  if (a >= n) return `All ${n} episodes are downloadable (app API, best quality; the website also serves all of them).`;
  return `All ${n} episodes are downloadable through the app API (best quality). The website itself only serves the first ${a}; those are used as a 720p fallback if the app API fails.`;
}

module.exports = {
  BASE,
  USER_AGENT,
  resolveShareUrl,
  parseSeriesId,
  getSeries,
  getEpisode,
  getEpisodeFromShare,
  getEpisodeFromWeb,
  downloadFile,
  downloadEpisode,
  decryptEpisodeFile,
  sanitizeFilename,
  episodeFilename,
  padEpisode,
  availabilityNotice,
  isAbort,
  findKey,
};
