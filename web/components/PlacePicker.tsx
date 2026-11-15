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
  /** So the landing can hide the place card while the list is open (no overlap). */
  onOpenChange?: (open: boolean) => void;
  reducedMotion?: boolean;
  className?: string;
};

export function PlacePicker({
  places,
  selectedId,
  onSelect,
  onOpenChange,
  reducedMotion = false,
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
    onOpenChange?.(open);
  }, [open, onOpenChange]);

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
      <p id={labelId} className="em-chrome-label mb-2">
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
        className="flex min-h-12 w-full items-center justify-between gap-3 rounded-full border border-white/12 bg-[#14181E] px-5 text-left text-[#F4F1EC] shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] transition hover:border-white/20 hover:bg-[#1C2229] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
      >
        <span className="min-w-0 flex-1 truncate">
          {selected ? (
            <span className="flex flex-col">
              <span className="font-ui text-[16px] font-semibold leading-tight tracking-tight">
                {primary}
              </span>
              {secondary ? (
                <span className="mt-0.5 text-[12px] text-[#7A766E]">
                  {secondary}
                </span>
              ) : null}
            </span>
          ) : (
            <span className="text-sm text-[#A8A49C]">Choose a place…</span>
          )}
        </span>
        <span
          className={`text-[10px] text-[#7A766E] transition-transform duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] ${open ? "rotate-180" : ""}`}
          aria-hidden
        >
          ▾
        </span>
      </button>

      <div
        className={`grid ${
          reducedMotion
            ? ""
            : "transition-[grid-template-rows,opacity] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)]"
        } ${open ? "grid-rows-[1fr] opacity-100" : "pointer-events-none grid-rows-[0fr] opacity-0"}`}
        inert={!open ? true : undefined}
        aria-hidden={!open}
      >
        <div className="min-h-0 overflow-hidden">
          <ul
            ref={listRef}
            id={listId}
            role="listbox"
            tabIndex={-1}
            aria-labelledby={labelId}
            aria-activedescendant={
              open ? `${listId}-opt-${activeIndex}` : undefined
            }
            onKeyDown={onListKeyDown}
            className="relative z-30 mt-2 max-h-[min(18rem,50vh)] overflow-y-auto rounded-xl border border-white/12 bg-[#14181E] py-2 shadow-[0_24px_64px_rgba(10,14,19,0.65)] outline-none"
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
                  className={`cursor-pointer px-4 py-3 transition-colors duration-150 ${
                    isActive || isSelected ? "bg-[#1C2229]" : "bg-transparent"
                  }`}
                >
                  <span
                    className={`font-ui block text-[16px] font-semibold leading-tight tracking-tight ${
                      isSelected ? "text-[#5BA8A0]" : "text-[#F4F1EC]"
                    }`}
                  >
                    {parts.primary}
                  </span>
                  <span className="mt-1.5 flex flex-wrap gap-x-2.5 text-[12px] text-[#7A766E]">
                    {parts.secondary ? <span>{parts.secondary}</span> : null}
                    <span>{place.theme}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </div>
  );
}
