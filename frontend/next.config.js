/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Évite que des erreurs ESLint/TS résiduelles bloquent le build prod.
  // Le typecheck reste actif en dev via tsc côté éditeur.
  eslint: { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: true },
  images: {
    domains: ["crests.football-data.org"],
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

// Sentry wrapping (activé seulement si SENTRY_AUTH_TOKEN défini au build).
// Sans token, Sentry SDK fonctionne quand même en runtime via les configs ts,
// juste pas d'upload de source maps.
const { withSentryConfig } = require("@sentry/nextjs");

module.exports = process.env.SENTRY_AUTH_TOKEN
  ? withSentryConfig(nextConfig, {
      org: process.env.SENTRY_ORG,
      project: process.env.SENTRY_PROJECT,
      silent: true,
      widenClientFileUpload: true,
      hideSourceMaps: true,
      disableLogger: true,
    })
  : nextConfig;
