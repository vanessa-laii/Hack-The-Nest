import path from "node:path";
import type { NextConfig } from "next";

const repoRoot = path.join(__dirname, "..");

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  // Include repo-root data/stories in the serverless bundle (play-only Vercel).
  outputFileTracingRoot: repoRoot,
  outputFileTracingIncludes: {
    "/*": ["../data/stories/**/*"],
    "/story/[id]": ["../data/stories/**/*"],
    "/api/narrate": ["../data/stories/**/*"],
    "/files/[...path]": ["../data/stories/**/*"],
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
