/**
 * Site footer with HL-11 credit lines + GitHub link.
 * Inputs: none.
 * Outputs: compact bordered credit footer (fits first viewport with landing).
 */

export function Footer() {
  return (
    <footer className="relative z-30 shrink-0 border-t border-white/[0.06] bg-[#121214] px-5 py-2.5 text-center sm:px-8 sm:py-3">
      <p className="mx-auto max-w-3xl text-[11px] leading-snug text-[#a1a1a6]">
        Landsat imagery courtesy of NASA and the U.S. Geological Survey.
      </p>
      <p className="mx-auto mt-1 max-w-3xl text-[11px] leading-snug text-[#a1a1a6]">
        Supported by the Michigan Space Grant Consortium, NASA grant
        80NSSC20M0124.
      </p>
      <p className="mt-1.5">
        <a
          href="https://github.com/vanessa-laii/Hack-The-Nest"
          target="_blank"
          rel="noopener noreferrer"
          className="font-label text-[10px] uppercase tracking-[0.2em] text-[#6e6e73] transition hover:text-[#2997ff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
        >
          GitHub Repository
        </a>
      </p>
    </footer>
  );
}
