/** @type {import('tailwindcss').Config} */
const v = (n) => `rgb(var(--${n}) / <alpha-value>)`;
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: v("bg"), sink: v("sink"), panel: v("panel"), panel2: v("panel-2"), raise: v("raise"),
        line: v("line"), line2: v("line-2"), line3: v("line-3"),
        fg: v("fg"), fg2: v("fg-2"), muted: v("muted"), faint: v("faint"),
        accent: v("accent"), "accent-hi": v("accent-hi"), "accent-ink": v("accent-ink"),
        up: v("up"), down: v("down"), warn: v("warn"), live: v("live"),
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', "system-ui", "-apple-system", '"Segoe UI"', "Roboto", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", '"SF Mono"', "Menlo", "Consolas", "monospace"],
      },
      fontSize: {
        "2xs": ["11px", "16px"], xs: ["12px", "18px"], sm: ["13px", "20px"],
        base: ["14px", "22px"], lg: ["16px", "24px"], xl: ["20px", "28px"], "2xl": ["26px", "32px"],
      },
      borderRadius: { sm: "3px", DEFAULT: "5px", md: "6px", lg: "8px" },
    },
  },
  plugins: [],
};
