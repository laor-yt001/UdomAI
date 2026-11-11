'use strict';

/**
 * Extract a balanced JSON object that follows `marker` inside an HTML/JS blob.
 * The site embeds `_ROUTER_DATA = {...}` followed by more JavaScript, so we
 * cannot simply JSON.parse up to </script>. This does a string-aware
 * balanced-brace scan starting at the first `{` after the marker.
 *
 * @param {string} html
 * @param {string} marker e.g. "_ROUTER_DATA = "
 * @returns {any} parsed JSON
 */
function extractJson(html, marker) {
  const s = html.indexOf(marker);
  if (s < 0) throw new Error('marker not found: ' + marker);
  const i = html.indexOf('{', s);
  if (i < 0) throw new Error('no object start after marker: ' + marker);
  let depth = 0;
  let inStr = false;
  let esc = false;
  for (let j = i; j < html.length; j++) {
    const c = html[j];
    if (inStr) {
      if (esc) esc = false;
      else if (c === '\\') esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return JSON.parse(html.slice(i, j + 1));
    }
  }
  throw new Error('unbalanced JSON after marker: ' + marker);
}

module.exports = { extractJson };
