/** Server sends the QR as a 0/1 module matrix; draw it as one SVG path.
 *  No image bytes, no external assets. */
export function QR({ matrix, size = 168 }: { matrix?: number[][] | null; size?: number }) {
  if (!matrix || !matrix.length) return null;
  const n = matrix.length;
  let d = "";
  for (let y = 0; y < n; y++) for (let x = 0; x < matrix[y].length; x++) if (matrix[y][x]) d += `M${x} ${y}h1v1h-1z`;
  return (
    <div className="flex-none rounded bg-white p-2" style={{ width: size, height: size }}>
      <svg viewBox={`0 0 ${n} ${n}`} width="100%" height="100%" shapeRendering="crispEdges" role="img" aria-label="Lightning invoice QR code">
        <path d={d} fill="#000" />
      </svg>
    </div>
  );
}
