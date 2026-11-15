import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  // Bundle cached films next to the Next app (Vercel Root Directory = web).
  outputFileTracingIncludes: {
    "/*": ["./data/stories/**/*"],
    "/story/[id]": ["./data/stories/**/*"],
    "/api/narrate": ["./data/stories/**/*"],
    "/files/[...path]": ["./data/stories/**/*"],
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
