import { defineConfig } from 'vitest/config';

/**
 * Vitest configuration for the frontend SPA.
 *
 * Runs unit and fast-check property tests under jsdom so DOM-rendering
 * behavior is testable without a browser.
 */
export default defineConfig({
  test: {
    environment: 'jsdom',
  },
});
