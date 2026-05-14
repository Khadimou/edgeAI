// Sentry config — côté client (browser).
// Charge le SDK uniquement si NEXT_PUBLIC_SENTRY_DSN est set en build.
import * as Sentry from "@sentry/nextjs";

const DSN = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (DSN) {
  Sentry.init({
    dsn: DSN,
    tracesSampleRate: 0.1,
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 1.0,
    environment: process.env.NODE_ENV,
    // Ignore les erreurs de network connues (cancelled, etc.)
    ignoreErrors: [
      "Network Error",
      "Failed to fetch",
      "AbortError",
    ],
  });
}
