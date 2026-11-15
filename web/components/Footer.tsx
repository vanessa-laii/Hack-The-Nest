/**
 * Site footer with HL-11 credit lines + GitHub link.
 * Inputs: none.
 * Outputs: compact bordered credit footer (fits first viewport with landing).
 */

export function Footer() {
  return (
    <footer className="relative z-30 shrink-0 border-t border-white/[0.06] bg-[#0A0E13] px-5 py-2.5 text-center sm:px-8 sm:py-3">
      <p className="mx-auto max-w-3xl text-[11px] leading-snug text-[#A8A49C]">
        Landsat imagery courtesy of NASA and the U.S. Geological Survey.
      </p>
      <p className="mx-auto mt-1 max-w-3xl text-[11px] leading-snug text-[#A8A49C]">
        Supported by the Michigan Space Grant Consortium, NASA grant
        80NSSC20M0124.
      </p>
      <p className="mt-1.5">
        <a
          href="https://github.com/vanessa-laii/Hack-The-Nest"
          target="_blank"
          rel="noopener noreferrer"
          className="text-[12px] text-[#7A766E] transition hover:text-[#5BA8A0] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
        >
          GitHub repository
        </a>
      </p>
    </footer>
  );
}
