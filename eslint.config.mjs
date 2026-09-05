import js from "@eslint/js";
import globals from "globals";

// The `globals` package marks writable globals with `true` and read-only ones
// with `false`, but ESLint 9 only accepts string values (a boolean `false`
// silently disables the global, turning every reference into no-undef).
// Translate the package's booleans into the string form ESLint understands.
function asEslintGlobals(pkgGlobals) {
  return Object.fromEntries(
    Object.entries(pkgGlobals).map(([name, writable]) => [
      name,
      writable ? "writable" : "readonly",
    ]),
  );
}

export default [
  {
    ignores: [
      // Mirrors .gitignore: never lint dependencies, caches, or build output.
      "dashboard_v2/fonts/**",
      "dashboard_v2/icons/**",
      "node_modules/**",
      ".venv/**",
      "dist/**",
      "build/**",
      "native/**/build*/**",
    ],
  },
  js.configs.recommended,
  {
    files: ["dashboard_v2/**/*.js"],
    rules: {
      // `catch (_)` is the established ignore-the-error idiom in this codebase.
      "no-unused-vars": ["error", { caughtErrorsIgnorePattern: "^_" }],
    },
    languageOptions: {
      ecmaVersion: "latest",
      // Loaded via <script> tag from index.html, not a module.
      sourceType: "script",
      globals: {
        ...asEslintGlobals(globals.browser),
        // Provided by index.html / qwebchannel.js / the Python bridge.
        bridge: "readonly", // window.bridge, QWebChannel-registered backend object
        AR: "writable", // window.AR = {...} app state, assigned bare throughout
        QWebChannel: "readonly", // qwebchannel.js injects the global constructor
        qt: "readonly", // qwebchannel.js transport object
      },
    },
  },
  {
    files: ["tests/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "commonjs",
      globals: asEslintGlobals(globals.node),
    },
  },
  {
    // CI helper scripts (scripts/ci/*.mjs) are plain Node ESM.
    files: ["scripts/**/*.mjs"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: asEslintGlobals(globals.node),
    },
  },
];
