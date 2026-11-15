/**
 * Smoke-test helper: POST /api/narrate for mock-narrate-demo.
 * Usage: npm run narrate:mock   (requires next dev on :3000)
 * Never prints secrets.
 */

const STORY_ID = process.env.NARRATE_STORY_ID || "mock-narrate-demo";
const BASE = process.env.NARRATE_BASE || "http://localhost:3000";

async function main() {
  const url = `${BASE.replace(/\/$/, "")}/api/narrate`;
  console.log(`POST ${url} { story_id: "${STORY_ID}" }`);

  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ story_id: STORY_ID }),
  });

  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    console.error("Non-JSON response", res.status, text.slice(0, 200));
    process.exit(1);
  }

  if (!res.ok) {
    console.error("FAILED", res.status, json?.error || json);
    process.exit(1);
  }

  console.log("OK");
  console.log("  story_id:", json.story_id);
  console.log("  voice_id:", json.voice_id);
  console.log("  model_id:", json.model_id);
  console.log("  total_duration_s:", json.total_duration_s);
  console.log("  segments:", json.segments?.length);
  for (const seg of json.segments || []) {
    console.log(
      `  - ${seg.segment_id}: ${seg.duration_s?.toFixed?.(2) ?? seg.duration_s}s, ${seg.words?.length ?? 0} words, ${seg.audio_url}`,
    );
  }
  console.log(`  cache path: data/stories/${STORY_ID}/narration/narration.json`);
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
