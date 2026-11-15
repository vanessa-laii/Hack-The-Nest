/**
 * Server-only ElevenLabs TTS-with-timestamps client.
 * Inputs: segment text + optional neighbor text for continuity.
 * Outputs: mp3 bytes + character alignment (never logs the API key).
 */

import type { CharAlignment } from "./align";

export type TtsWithTimestampsResult = {
  audio: Buffer;
  alignment: CharAlignment;
};

type ElevenLabsResponse = {
  audio_base64?: string;
  alignment?: CharAlignment | null;
  normalized_alignment?: CharAlignment | null;
};

const TTS_TIMEOUT_MS = 45_000;

export function getElevenLabsConfig(): {
  apiKey: string;
  voiceId: string;
  modelId: string;
} {
  const apiKey = process.env.ELEVENLABS_API_KEY?.trim();
  const voiceId = process.env.ELEVENLABS_VOICE_ID?.trim();
  const modelId = process.env.ELEVENLABS_MODEL_ID?.trim() || "eleven_multilingual_v2";

  if (!apiKey || !voiceId) {
    throw new Error("Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID");
  }

  return { apiKey, voiceId, modelId };
}

export async function synthesizeSegment(opts: {
  text: string;
  previousText?: string;
  nextText?: string;
}): Promise<TtsWithTimestampsResult> {
  const { apiKey, voiceId, modelId } = getElevenLabsConfig();
  const url = `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(voiceId)}/with-timestamps?output_format=mp3_44100_128`;

  const body: Record<string, unknown> = {
    text: opts.text,
    model_id: modelId,
    voice_settings: {
      stability: 0.75,
      similarity_boost: 0.7,
      style: 0.0,
      use_speaker_boost: true,
      // Under 1.0 so ~150-word scripts land in the 55–95 s schema / HL-1 band.
      // 0.95 produced ~53 s on mock-narrate-demo; 0.88 targets ≥55 s.
      speed: 0.88,
    },
    apply_text_normalization: "auto",
  };
  if (opts.previousText) body.previous_text = opts.previousText;
  if (opts.nextText) body.next_text = opts.nextText;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TTS_TIMEOUT_MS);

  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "xi-api-key": apiKey,
        Accept: "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    const name = err instanceof Error ? err.name : "";
    if (name === "AbortError") {
      throw new Error("ElevenLabs request timed out");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    // Do not include response body if it might echo request headers; keep status only.
    throw new Error(`ElevenLabs HTTP ${res.status}`);
  }

  const data = (await res.json()) as ElevenLabsResponse;
  if (!data.audio_base64) {
    throw new Error("ElevenLabs response missing audio_base64");
  }
  if (!data.alignment?.characters?.length) {
    throw new Error("ElevenLabs response missing character alignment");
  }

  return {
    audio: Buffer.from(data.audio_base64, "base64"),
    alignment: data.alignment,
  };
}
