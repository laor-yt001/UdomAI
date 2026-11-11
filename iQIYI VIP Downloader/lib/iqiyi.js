'use strict';

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';

function findFfmpeg() {
  if (process.env.FFMPEG_PATH && fs.existsSync(process.env.FFMPEG_PATH)) {
    return process.env.FFMPEG_PATH;
  }
  const rootDir = path.resolve(__dirname, '..', '..');
  const scanDirs = [
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python'),
    path.join(rootDir, 'venv', 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries'),
    path.join(rootDir, 'venv', 'lib', 'python*', 'site-packages', 'imageio_ffmpeg', 'binaries'),
    path.join(rootDir, 'venv', 'Scripts'),
    path.join(rootDir, 'venv', 'bin'),
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
    path.join(rootDir, 'venv', 'bin', 'ffmpeg'),
    path.join(__dirname, '..', '..', 'ffmpeg', 'ffmpeg.exe'),
    'ffmpeg.exe',
    'ffmpeg'
  ];
  for (const c of candidates) {
    try {
      if (path.isAbsolute(c) && fs.existsSync(c)) {
        return c;
      }
    } catch {}
  }
  return 'ffmpeg';
}

function getCookiesString(bid = '600') {
  // Cookie file path: honour IQIYI_COOKIE_FILE env var (set by Python when cookies
  // are injected from COOKIES_IQIYI / COOKIES_ALL env vars), otherwise fall back
  // to the default cookies.txt beside the repo root.
  const rootDir = path.resolve(__dirname, '..', '..');
  const cookiePath = process.env.IQIYI_COOKIE_FILE || path.join(rootDir, 'cookies.txt');
  const cookieMap = new Map();
  cookieMap.set('lang', 'en_us');
  cookieMap.set('mod', 'intl');
  cookieMap.set('QiyiPlayerBID', bid);

  if (fs.existsSync(cookiePath)) {
    try {
      const content = fs.readFileSync(cookiePath, 'utf8');
      const lines = content.split('\n');
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const parts = trimmed.split('\t');
        if (parts.length >= 7) {
          const domain = parts[0].trim().toLowerCase();
          if (domain.includes('iq.com') || domain.includes('iqiyi.com') || domain === '') {
            const name = parts[5].trim();
            const value = parts[6].trim();
            if (name && value && name !== 'QiyiPlayerBID') {
              cookieMap.set(name, value);
            }
          }
        } else if (trimmed.includes('=')) {
          // Fallback: header-style "name=value; name2=val2" or line-by-line "name=value"
          const pairs = trimmed.split(';');
          for (const p of pairs) {
            const eqIdx = p.indexOf('=');
            if (eqIdx > 0) {
              const k = p.substring(0, eqIdx).trim();
              const v = p.substring(eqIdx + 1).trim();
              if (k && v && k !== 'QiyiPlayerBID' && !k.startsWith('#')) {
                cookieMap.set(k, v);
              }
            }
          }
        }
      }
    } catch {}
  }

  const result = [];
  for (const [k, v] of cookieMap.entries()) {
    result.push(`${k}=${v}`);
  }
  return result.join('; ');
}

function sanitizeName(name) {
  return String(name || 'iQIYI Series')
    .replace(/[\\/:*?"<>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function episodeFilename(seriesName, episodeNumber, totalEpisodes) {
  const cleanSeries = sanitizeName(seriesName);
  const padLen = Math.max(2, String(totalEpisodes || 10).length);
  const epPad = String(episodeNumber).padStart(padLen, '0');
  return `${cleanSeries} - EP${epPad}.mp4`;
}

function cleanUrl(url) {
  if (!url) return '';
  url = url.trim();
  if (!url.startsWith('http')) {
    if (url.startsWith('//')) {
      url = 'https:' + url;
    } else if (url.includes('iq.com')) {
      url = 'https://' + url;
    } else {
      url = 'https://www.iq.com/album/' + url;
    }
  }
  return url;
}

async function fetchPage(url, extraCookie, bid = '600') {
  let cookies = getCookiesString(bid);
  if (extraCookie) {
    cookies += '; ' + extraCookie;
  }
  const res = await fetch(url, {
    headers: {
      'User-Agent': USER_AGENT,
      'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'Accept-Language': 'en-US,en;q=0.9',
      'Cookie': cookies,
      'Referer': 'https://www.iq.com/'
    }
  });
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url} (HTTP ${res.status})`);
  }
  return await res.text();
}

function extractNextData(html) {
  const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
  if (!m) {
    throw new Error('Unable to find __NEXT_DATA__ in page HTML.');
  }
  return JSON.parse(m[1]);
}

async function getSeries(rawUrl, signal) {
  const url = cleanUrl(rawUrl);
  const html = await fetchPage(url);
  const nextData = extractNextData(html);
  const props = nextData.props || {};
  const initState = props.initialState || {};

  let albumInfo = null;
  if (initState.album && initState.album.videoAlbumInfo && initState.album.videoAlbumInfo.albumId) {
    albumInfo = initState.album.videoAlbumInfo;
  } else if (initState.play && initState.play.albumInfo && initState.play.albumInfo.albumId) {
    albumInfo = initState.play.albumInfo;
  }

  if (!albumInfo) {
    throw new Error('Could not find series album information.');
  }

  const seriesId = String(albumInfo.albumId || albumInfo.qipuId || '');
  const seriesName = albumInfo.name || albumInfo.albumName || 'iQIYI Series';
  let cover = albumInfo.thumbnailUrl1 || albumInfo.albumPic260 || albumInfo.albumPic284 || albumInfo.schemaAlbumImage || albumInfo.albumFocus1024 || '';
  if (cover.startsWith('//')) cover = 'https:' + cover;

  const intro = albumInfo.desc || '';
  const totalEpisodes = Number(albumInfo.total || albumInfo.maxOrder || 0);

  // Episodes map: order -> episode object
  const episodesMap = new Map();

  // 1. Initial cached episodes
  const cached = (initState.album && initState.album.cacheAlbumList && initState.album.cacheAlbumList['1']) ||
                 (initState.play && initState.play.cachePlayList && initState.play.cachePlayList['1']) || [];
  for (const ep of cached) {
    const epNum = Number(ep.order || ep.orderRseat || (episodesMap.size + 1));
    const vid = String(ep.qipuIdStr || ep.qipuId || ep.vid || '');
    if (!vid) continue;
    let epCover = ep.imageLoader || ep.imgUrl || ep.albumPic284 || '';
    if (epCover.startsWith('//')) epCover = 'https:' + epCover;

    episodesMap.set(epNum, {
      vid,
      episode: epNum,
      title: ep.subTitle || ep.name || `Episode ${epNum}`,
      tvid: String(ep.tvId || ep.qipuId || ''),
      cover: epCover,
      playUrl: ep.albumPlayUrl ? (ep.albumPlayUrl.startsWith('//') ? 'https:' + ep.albumPlayUrl : ep.albumPlayUrl) : `https://www.iq.com/play/${vid}?lang=en_us`,
      filename: episodeFilename(seriesName, epNum, totalEpisodes || cached.length)
    });
  }

  // 2. Fetch additional pages if available
  const ranges = albumInfo.totalPageRange || albumInfo.subPageRange || [];
  for (const range of ranges) {
    if (range.pageNo > 1) {
      try {
        const apiUrl = `https://pcw-api.iq.com/api/episodeListSource/${seriesId}?platformId=3&modeCode=intl&langCode=en_us&startOrder=${range.from}&endOrder=${range.to}`;
        const res = await fetch(apiUrl, {
          headers: { 'User-Agent': USER_AGENT, 'Referer': 'https://www.iq.com/' },
          signal
        });
        if (res.ok) {
          const data = await res.json();
          const epg = (data.data && data.data.epg) || [];
          for (const ep of epg) {
            const epNum = Number(ep.order || (episodesMap.size + 1));
            const vid = String(ep.qipuIdStr || ep.qipuId || ep.vid || '');
            if (!vid) continue;
            let epCover = ep.imageLoader || ep.imgUrl || ep.albumPic284 || '';
            if (epCover.startsWith('//')) epCover = 'https:' + epCover;

            episodesMap.set(epNum, {
              vid,
              episode: epNum,
              title: ep.subTitle || ep.name || `Episode ${epNum}`,
              tvid: String(ep.tvId || ep.qipuId || ''),
              cover: epCover,
              playUrl: ep.playLocSuffix ? `https://www.iq.com/play/${ep.playLocSuffix}` : `https://www.iq.com/play/${vid}?lang=en_us`,
              filename: episodeFilename(seriesName, epNum, totalEpisodes || (episodesMap.size + 1))
            });
          }
        }
      } catch (e) {
        // Continue best effort
      }
    }
  }

  const episodes = Array.from(episodesMap.values()).sort((a, b) => a.episode - b.episode);
  const count = totalEpisodes || episodes.length;

  for (const ep of episodes) {
    ep.filename = episodeFilename(seriesName, ep.episode, count);
    if (!ep.title || ep.title === `Episode ${ep.episode}`) {
      ep.title = `EP${String(ep.episode).padStart(String(count).length || 2, '0')} - ${seriesName}`;
    }
  }

  return {
    seriesId,
    name: seriesName,
    cover,
    intro,
    episodeCount: count,
    accessibleCount: episodes.length,
    vids: episodes.map(e => e.vid),
    episodes
  };
}

async function extractM3u8FromPlay(playUrl) {
  // Try qualities in order: 600 (1080P), 500 (720P HD), 300 (480P), 200 (360P)
  const bids = ['600', '500', '300', '200'];
  let vipBlocked = false;
  let lastError = null;

  for (const bid of bids) {
    try {
      const html = await fetchPage(playUrl, null, bid);
      const nextData = extractNextData(html);
      const prePlayer = nextData.props?.initialProps?.pageProps?.prePlayerData;
      const dashData = prePlayer?.dash?.data;
      if (!dashData) continue;

      if (dashData.boss_ts && (dashData.boss_ts.code === 'Q00505' || dashData.ctl?.boss_not_pass)) {
        vipBlocked = true;
      }

      const videos = dashData.program?.video || [];
      for (const v of videos) {
        if (v && v.m3u8 && typeof v.m3u8 === 'string' && v.m3u8.includes('#EXTM3U')) {
          return {
            m3u8: v.m3u8,
            bid: v.bid,
            scrsz: v.scrsz,
            vsize: v.vsize,
            duration: v.duration
          };
        }
      }
    } catch (err) {
      lastError = err;
    }
  }

  if (vipBlocked) {
    throw new Error('VIP-only episode: This episode requires an active iQIYI VIP account. Please add your iQIYI VIP cookies to cookies.txt.');
  }

  throw new Error(lastError ? lastError.message : 'Unable to extract playable stream (m3u8) from iQIYI play page.');
}

async function downloadEpisode(seriesId, vid, destPath, options = {}) {
  let playUrl = options.playUrl;
  if (!playUrl) {
    if (vid.startsWith('http')) {
      playUrl = vid;
    } else {
      playUrl = `https://www.iq.com/play/${vid}?lang=en_us`;
    }
  }

  const streamInfo = await extractM3u8FromPlay(playUrl);

  await fsp.mkdir(path.dirname(destPath), { recursive: true });

  const tempM3u8 = path.join(os.tmpdir(), `iqiyi_${Date.now()}_${Math.random().toString(36).slice(2)}.m3u8`);
  await fsp.writeFile(tempM3u8, streamInfo.m3u8, 'utf8');

  const ffmpegBin = findFfmpeg();

  return new Promise((resolve, reject) => {
    const httpHeaders = `User-Agent: ${USER_AGENT}\r\nReferer: https://www.iq.com/\r\nOrigin: https://www.iq.com\r\n`;
    const args = [
      '-y',
      '-headers', httpHeaders,
      '-protocol_whitelist', 'file,http,https,tcp,tls',
      '-i', tempM3u8,
      '-c', 'copy',
      '-bsf:a', 'aac_adtstoasc',
      '-movflags', '+faststart',
      destPath
    ];

    const proc = spawn(ffmpegBin, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = '';

    proc.stderr.on('data', chunk => {
      stderr += chunk.toString();
    });

    proc.on('close', async code => {
      try {
        await fsp.unlink(tempM3u8);
      } catch {}

      if (code === 0 && fs.existsSync(destPath)) {
        const stats = fs.statSync(destPath);
        resolve({
          ok: true,
          bytes: stats.size,
          path: destPath,
          quality: streamInfo.bid,
          resolution: streamInfo.scrsz
        });
      } else {
        reject(new Error(`ffmpeg exited with code ${code}: ${stderr.slice(-300)}`));
      }
    });

    proc.on('error', async err => {
      try {
        await fsp.unlink(tempM3u8);
      } catch {}
      reject(err);
    });
  });
}

module.exports = {
  getSeries,
  downloadEpisode,
  extractM3u8FromPlay,
  episodeFilename,
  findFfmpeg,
  sanitizeName
};

if (require.main === module) {
  const testUrl = process.argv[2] || 'https://www.iq.com/album/celestial-above-all-2025-22le6kky0zp?lang=en_us';
  (async () => {
    console.log(`Fetching series: ${testUrl}`);
    const series = await getSeries(testUrl);
    console.log(`Found: ${series.name} (${series.episodeCount} episodes)`);
    console.log(`Sample EP1:`, series.episodes[0]);
  })().catch(e => console.error(e));
}
