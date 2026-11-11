'use strict';

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';

function findFfmpeg() {
  const rootDir = path.resolve(__dirname, '..', '..');
  const candidates = [
    path.join(rootDir, 'venv', 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries', 'ffmpeg.exe'),
    path.join(rootDir, 'venv', 'Scripts', 'ffmpeg.exe'),
    'ffmpeg.exe',
    'ffmpeg'
  ];
  for (const c of candidates) {
    if (path.isAbsolute(c) && fs.existsSync(c)) {
      return c;
    }
  }
  return 'ffmpeg';
}

function findCookieFile() {
  const rootDir = path.resolve(__dirname, '..', '..');
  const candidates = [
    path.join(rootDir, 'wetv_cookies.txt'),
    path.join(rootDir, 'cookies.txt'),
    path.join(rootDir, 'cookies_wetv.txt')
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) {
      try {
        const stat = fs.statSync(c);
        if (stat.size > 10) return c;
      } catch {}
    }
  }
  return null;
}

function sanitizeName(name) {
  return String(name || 'WeTV Series')
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
    } else if (url.includes('wetv.vip')) {
      url = 'https://' + url;
    } else {
      url = 'https://wetv.vip/en/play/' + url;
    }
  }
  // Convert /album/ to /play/
  url = url.replace(/\/album\//, '/play/');
  return url;
}

async function fetchPage(url) {
  const cookieFile = findCookieFile();
  let cookieHeader = '';
  if (cookieFile) {
    try {
      const content = fs.readFileSync(cookieFile, 'utf8');
      const lines = content.split('\n');
      const pairs = [];
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const parts = trimmed.split('\t');
        if (parts.length >= 7) {
          const dom = parts[0].toLowerCase();
          if (dom.includes('wetv') || dom.includes('qq.com')) {
            pairs.push(`${parts[5]}=${parts[6]}`);
          }
        }
      }
      if (pairs.length > 0) {
        cookieHeader = pairs.join('; ');
      }
    } catch {}
  }

  const headers = {
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://wetv.vip/'
  };
  if (cookieHeader) {
    headers['Cookie'] = cookieHeader;
  }

  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url} (HTTP ${res.status})`);
  }
  return await res.text();
}

function extractNextData(html) {
  const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
  if (!m) {
    throw new Error('Unable to find __NEXT_DATA__ in WeTV page HTML.');
  }
  return JSON.parse(m[1]);
}

async function getSeries(rawUrl, signal) {
  const url = cleanUrl(rawUrl);
  const html = await fetchPage(url);
  const nextData = extractNextData(html);
  const props = nextData.props || {};
  const pageProps = props.pageProps || {};
  let data = pageProps.data || {};
  if (typeof data === 'string') {
    try {
      data = JSON.parse(data);
    } catch {}
  }

  const coverInfo = data.coverInfo || {};
  let seriesId = coverInfo.cid || '';
  if (!seriesId) {
    const m = url.match(/\/play\/([a-zA-Z0-9]+)/);
    if (m) seriesId = m[1];
  }

  const seriesName = coverInfo.title || 'WeTV Series';
  let cover = coverInfo.posterHz || coverInfo.posterVt || coverInfo.cover_image || '';
  if (cover.startsWith('//')) cover = 'https:' + cover;

  const intro = coverInfo.description || '';
  const videoList = data.videoList || [];
  const totalEpisodes = Number(coverInfo.episodeAll || coverInfo.episodeUpdated || videoList.length);

  const episodes = [];
  for (let idx = 0; idx < videoList.length; idx++) {
    const v = videoList[idx];
    const vid = String(v.vid || '');
    if (!vid) continue;

    let epNum = idx + 1;
    if (v.episode && !isNaN(Number(v.episode))) {
      epNum = Number(v.episode);
    }

    const isFree = Boolean(v.isFree);
    const isVip = !isFree || v.payStatus === 6 || v.payStatus === 7;

    let epCover = v.pic_640_360 || v.pic_496_280 || v.pic_332_187 || cover;
    if (epCover.startsWith('//')) epCover = 'https:' + epCover;

    const playUrl = (seriesId && vid) ? `https://wetv.vip/en/play/${seriesId}/${vid}` : url;
    const count = totalEpisodes || videoList.length;

    episodes.push({
      vid,
      episode: epNum,
      title: v.title || `EP${String(epNum).padStart(2, '0')}: ${seriesName}`,
      isFree,
      isVip,
      duration: Number(v.duration || 0),
      cover: epCover,
      playUrl,
      filename: episodeFilename(seriesName, epNum, count)
    });
  }

  episodes.sort((a, b) => a.episode - b.episode);

  return {
    seriesId,
    name: seriesName,
    cover,
    intro,
    episodeCount: episodes.length,
    accessibleCount: episodes.length,
    vids: episodes.map(e => e.vid),
    episodes
  };
}

async function downloadEpisode(seriesId, vid, destPath, options = {}) {
  let playUrl = options.playUrl;
  if (!playUrl) {
    if (vid.startsWith('http')) {
      playUrl = vid;
    } else if (seriesId) {
      playUrl = `https://wetv.vip/en/play/${seriesId}/${vid}`;
    } else {
      playUrl = `https://wetv.vip/en/play/${vid}`;
    }
  }

  await fsp.mkdir(path.dirname(destPath), { recursive: true });

  const ffmpegBin = findFfmpeg();
  const ffmpegDir = path.dirname(ffmpegBin);
  const cookieFile = findCookieFile();

  const pythonBin = process.platform === 'win32'
    ? (fs.existsSync(path.resolve(__dirname, '..', '..', 'venv', 'Scripts', 'python.exe'))
        ? path.resolve(__dirname, '..', '..', 'venv', 'Scripts', 'python.exe')
        : 'python')
    : 'python3';

  // Construct Python script invocation with yt-dlp
  const pyCode = `
import sys, os, yt_dlp
ff_dir = r"${ffmpegDir}"
os.environ['PATH'] = ff_dir + os.pathsep + os.environ.get('PATH', '')
opts = {
    'quiet': True,
    'no_warnings': True,
    'nocheckcertificate': True,
    'ffmpeg_location': ff_dir,
    'outtmpl': r"${destPath}",
    'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
    'merge_output_format': 'mp4',
    'retries': 5,
    'socket_timeout': 30,
}
cookie_file = r"${cookieFile || ''}"
if cookie_file and os.path.exists(cookie_file):
    opts['cookiefile'] = cookie_file

with yt_dlp.YoutubeDL(opts) as ydl:
    ydl.download([r"${playUrl}"])
`;

  return new Promise((resolve, reject) => {
    const proc = spawn(pythonBin, ['-c', pyCode], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = '';

    proc.stderr.on('data', chunk => {
      stderr += chunk.toString();
    });

    proc.on('close', code => {
      if (code === 0 && fs.existsSync(destPath)) {
        const stats = fs.statSync(destPath);
        resolve({
          ok: true,
          bytes: stats.size,
          path: destPath
        });
      } else {
        // If file was created with slightly different extension (e.g. .mp4 already)
        if (fs.existsSync(destPath)) {
          const stats = fs.statSync(destPath);
          return resolve({ ok: true, bytes: stats.size, path: destPath });
        }
        reject(new Error(`Download failed (code ${code}): ${stderr.slice(-300)}`));
      }
    });

    proc.on('error', err => {
      reject(err);
    });
  });
}

module.exports = {
  getSeries,
  downloadEpisode,
  episodeFilename,
  findFfmpeg,
  findCookieFile,
  sanitizeName,
  cleanUrl
};

if (require.main === module) {
  const testUrl = process.argv[2] || 'https://wetv.vip/en/play/air11ooo2rdsdi3-Cute-Programmer';
  (async () => {
    console.log(`Fetching WeTV series: ${testUrl}`);
    const series = await getSeries(testUrl);
    console.log(`Found: ${series.name} (${series.episodeCount} episodes)`);
    console.log(`Sample EP1:`, series.episodes[0]);
  })().catch(e => console.error(e));
}

