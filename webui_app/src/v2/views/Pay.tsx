// A Lightning payment code as /v2 shows it everywhere it shows one (signal
// days, a tip, funding the agent's wallet): the QR code, the code itself,
// Copy, and Open in a wallet app. The owner pays from their own wallet app;
// nothing here pays anything.
import { useState, type ReactNode } from "react";

export function QrCode({ m }: { m: number[][] }) {
  const n = m.length;
  let d = "";
  for (let y = 0; y < n; y++) for (let x = 0; x < m[y].length; x++) if (m[y][x]) d += `M${x} ${y}h1v1h-1z`;
  return (
    <div className="qr">
      <svg viewBox={`0 0 ${n} ${n}`} width="100%" height="100%" shapeRendering="crispEdges" role="img" aria-label="Payment code (Lightning invoice)">
        <path d={d} fill="#000" />
      </svg>
    </div>
  );
}

/** The QR code (when the agent could make one), the facts above the code,
 *  the code, Copy and Open in a wallet app. */
export function PayCode({ bolt11, qr, children }: { bolt11: string; qr?: number[][] | null; children?: ReactNode }) {
  const [copied, setCopied] = useState("");
  return (
    <div className="pay">
      {qr && qr.length > 0 && <QrCode m={qr} />}
      <div style={{ display: "grid", gap: 12, minWidth: 0 }}>
        {children}
        <code className="bolt">{bolt11}</code>
        <div className="editor-row">
          <button
            className="btn btn-sm"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(bolt11);
                setCopied("Copied");
              } catch {
                setCopied("This browser blocked copying: select the code and copy it.");
              }
            }}
          >
            Copy the code
          </button>
          <a className="btn btn-sm" href={`lightning:${bolt11}`}>
            Open in a wallet app
          </a>
          {copied && (
            <span className="small" role="status">
              {copied}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
