// Tiny, safe markdown for the agreement: escape EVERYTHING first, then add
// only headings, bold, inline code, quotes, bullets and paragraphs.
function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
export function mdLite(src: string): string {
  const inl = (t: string) => t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  return esc(src || "")
    .split(/\n\s*\n/)
    .map((block) => {
      const ls = block.split("\n");
      if (ls.every((l) => /^\s*[-*]\s+/.test(l)))
        return "<ul>" + ls.map((l) => "<li>" + inl(l.replace(/^\s*[-*]\s+/, "")) + "</li>").join("") + "</ul>";
      const h = block.match(/^(#{1,4})\s+(.*)$/);
      if (h && ls.length === 1) return `<h${h[1].length + 2}>${inl(h[2])}</h${h[1].length + 2}>`;
      if (ls.every((l) => /^&gt;/.test(l)))
        return "<blockquote>" + inl(ls.map((l) => l.replace(/^&gt;\s?/, "")).join(" ")) + "</blockquote>";
      return "<p>" + inl(ls.join(" ")) + "</p>";
    })
    .join("");
}
