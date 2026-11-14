/**
 * Dark custom place dropdown for the landing.
 * Inputs: places list, selected id, onChange.
 * Outputs: accessible listbox that selects a place (same path as marker click).
 */

"use client";

import { useEffect, useId, useRef, useState } from "react";
import { splitPlaceName, type PlaceLocation } from "@/lib/places";

type PlacePickerProps = {
  places: PlaceLocation[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  className?: string;
};

export function PlacePicker({
  places,
  selectedId,
  onSelect,
  className,
}: PlacePickerProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const listId = useId();
  const labelId = useId();

  const selected = places.find((p) => p.id === selectedId) ?? null;
  const { primary, secondary } = selected
    ? splitPlaceName(selected.displayName)
    : { primary: null, secondary: null };

  useEffect(() => {
    if (!open) return;

    function onPointerDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const idx = Math.max(
      0,
      places.findIndex((p) => p.id === selectedId),
    );
    setActiveIndex(idx === -1 ? 0 : idx);
    requestAnimationFrame(() => listRef.current?.focus());
  }, [open, places, selectedId]);

  function choose(id: string) {
    onSelect(id);
    setOpen(false);
  }

  function onTriggerKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen(true);
    }
  }

  function onListKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(places.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(0, i - 1));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActiveIndex(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActiveIndex(places.length - 1);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const place = places[activeIndex];
      if (place) choose(place.id);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  return (
    <div ref={rootRef} className={className}>
      <p
        id={labelId}
        className="font-label mb-2 text-[10px] uppercase tracking-[0.28em] text-[#a1a1a6]"
      >
        Choose a place
      </p>

      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        aria-labelledby={labelId}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onTriggerKeyDown}
        className="flex min-h-12 w-full items-center justify-between gap-3 rounded-full border border-white/12 bg-[#1a1a1d]/80 px-5 text-left text-[#f5f5f7] shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] backdrop-blur-md transition hover:border-white/20 hover:bg-[#222226]/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
      >
        <span className="min-w-0 flex-1 truncate">
          {selected ? (
            <span className="flex flex-col">
              <span className="font-ui text-[16px] font-semibold leading-tight tracking-tight">
                {primary}
              </span>
              {secondary ? (
                <span className="font-label mt-0.5 text-[9px] uppercase tracking-[0.16em] text-[#6e6e73]">
                  {secondary}
                </span>
              ) : null}
            </span>
          ) : (
            <span className="text-sm text-[#a1a1a6]">Choose a place…</span>
          )}
        </span>
        <span
          className={`font-label text-[10px] text-[#6e6e73] transition ${open ? "rotate-180" : ""}`}
          aria-hidden
        >
          ▾
        </span>
      </button>

      {open ? (
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          tabIndex={-1}
          aria-labelledby={labelId}
          aria-activedescendant={`${listId}-opt-${activeIndex}`}
          onKeyDown={onListKeyDown}
          className="mt-2 max-h-72 overflow-y-auto rounded-2xl border border-white/12 bg-[#1a1a1d]/96 py-2 shadow-[0_24px_64px_rgba(18,18,20,0.55)] backdrop-blur-xl outline-none"
        >
          {places.map((place, i) => {
            const parts = splitPlaceName(place.displayName);
            const isSelected = place.id === selectedId;
            const isActive = i === activeIndex;

            return (
              <li
                key={place.id}
                id={`${listId}-opt-${i}`}
                role="option"
                aria-selected={isSelected}
                onMouseEnter={() => setActiveIndex(i)}
                onClick={() => choose(place.id)}
                className={`flex cursor-pointer items-start gap-3 px-4 py-3 transition ${
                  isActive ? "bg-[#222226]" : "bg-transparent"
                }`}
              >
                <span
                  className={`mt-2.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                    isSelected
                      ? "bg-[#2997ff] shadow-[0_0_8px_rgba(41,151,255,0.85)]"
                      : "bg-white/25"
                  }`}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="font-ui block text-[16px] font-semibold leading-tight tracking-tight text-[#f5f5f7]">
                    {parts.primary}
                  </span>
                  <span className="mt-1.5 flex flex-wrap gap-x-2.5 font-label text-[9px] uppercase tracking-[0.14em] text-[#6e6e73]">
                    {parts.secondary ? <span>{parts.secondary}</span> : null}
                    <span>{place.theme}</span>
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
