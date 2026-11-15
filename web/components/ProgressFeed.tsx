/**
 * Light SSE progress feed for /build/[id] (Phase 0).
 * Inputs: story id + optional events_url from createStory.
 * Outputs: live message list; redirects to /story/[id] on done.
 */

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { eventsUrl, fileUrl } from "@/lib/api";
import type { BuildEvent } from "@/lib/types/event";

type ProgressFeedProps = {
  storyId: string;
  eventsPath?: string | null;
};

type FeedLine = {
  seq: number;
  type: string;
  message: string;
  thumbUrl?: string;
};

export function ProgressFeed({ storyId, eventsPath }: ProgressFeedProps) {
  const router = useRouter();
  const [lines, setLines] = useState<FeedLine[]>([]);
  const [status, setStatus] = useState<"connecting" | "live" | "done" | "error">(
    "connecting",
  );
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const lastSeq = useRef(-1);

  useEffect(() => {
    const url =
      eventsPath && eventsPath.startsWith("http")
        ? eventsPath
        : eventsPath
          ? fileUrl(eventsPath)
          : eventsUrl(storyId);

    const es = new EventSource(url);
    setStatus("connecting");

    function onAny(ev: MessageEvent, typeFallback?: string) {
      let data: BuildEvent;
      try {
        data = JSON.parse(ev.data) as BuildEvent;
      } catch {
        return;
      }
      if (typeof data.seq === "number") lastSeq.current = data.seq;
      const type = data.type || typeFallback || "progress";
      setStatus("live");
      setLines((prev) => [
        ...prev,
        {
          seq: data.seq,
          type,
          message: data.message,
          thumbUrl: data.frame?.thumb_url
            ? fileUrl(data.frame.thumb_url)
            : undefined,
        },
      ]);

      if (type === "done") {
        setStatus("done");
        es.close();
        router.push(`/story/${storyId}`);
      } else if (type === "error") {
        setStatus("error");
        setErrorMsg(data.error?.message ?? data.message);
        es.close();
      }
    }

    es.onopen = () => setStatus("live");
    es.onmessage = (ev) => onAny(ev);
    es.addEventListener("progress", (ev) =>
      onAny(ev as MessageEvent, "progress"),
    );
    es.addEventListener("frame", (ev) => onAny(ev as MessageEvent, "frame"));
    es.addEventListener("change", (ev) => onAny(ev as MessageEvent, "change"));
    es.addEventListener("done", (ev) => onAny(ev as MessageEvent, "done"));
    es.addEventListener("error", (ev) => {
      // EventSource "error" can be network OR a named SSE event.
      if (ev instanceof MessageEvent && ev.data) {
        onAny(ev, "error");
        return;
      }
      // After a terminal done/error we already closed; ignore reconnect noise.
      if (es.readyState === EventSource.CLOSED) return;
      // Persistent network failure: close so the browser does not loop forever.
      if (es.readyState === EventSource.CONNECTING && lastSeq.current < 0) {
        // Still first connect — allow a brief retry window via browser default.
        return;
      }
    });

    // Always close on unmount; also closed explicitly on done/error above
    // so the browser does not auto-reconnect after the stream ends (Harman note).
    return () => es.close();
  }, [storyId, eventsPath, router]);

  return (
    <div className="relative z-10 mt-8">
      <p className="em-chrome-label text-[#5BA8A0]">
        {status === "connecting" && "Connecting…"}
        {status === "live" && "Live progress"}
        {status === "done" && "Done — opening story"}
        {status === "error" && "Build failed"}
      </p>

      {errorMsg ? (
        <p role="alert" className="mt-3 text-sm text-[#C4A574]">
          {errorMsg}{" "}
          <Link href="/" className="underline hover:text-[#5BA8A0]">
            ← Back to globe
          </Link>
        </p>
      ) : null}

      <ol
        className="mt-6 max-h-[28rem] space-y-2 overflow-y-auto"
        aria-live="polite"
        aria-label="Build events"
      >
        {lines.length === 0 ? (
          <li className="text-sm text-[#A8A49C]">Waiting for events…</li>
        ) : (
          lines.map((line) => (
            <li
              key={`${line.seq}-${line.type}`}
              className="flex items-start gap-3 rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 text-sm text-[#A8A49C]"
            >
              <span className="font-label shrink-0 text-[11px] tabular-nums text-[#7A766E]">
                {String(line.seq).padStart(2, "0")}
              </span>
              <span className="min-w-0 flex-1">
                <span className="font-label mr-2 text-[11px] tabular-nums text-[#5BA8A0]/80">
                  {line.type}
                </span>
                {line.message}
              </span>
              {line.thumbUrl ? (
                // eslint-disable-next-line @next/next/no-img-element -- remote mock thumbs
                <img
                  src={line.thumbUrl}
                  alt=""
                  width={40}
                  height={40}
                  className="h-10 w-10 shrink-0 rounded object-cover"
                />
              ) : null}
            </li>
          ))
        )}
      </ol>

      <p className="mt-6 text-sm text-[#7A766E]">
        Or{" "}
        <Link
          href={`/story/${storyId}`}
          className="text-[#5BA8A0] underline-offset-2 hover:underline"
        >
          open the story now
        </Link>{" "}
        (if the build already finished, the film may be playable).
      </p>
    </div>
  );
}
