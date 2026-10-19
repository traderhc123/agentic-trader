export function Logo({ className = "h-6 w-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x=".5" y=".5" width="23" height="23" rx="5" fill="rgb(17 26 41)" stroke="rgb(54 72 100)" />
      <path d="M5 16l4.5-5 3 2.5L19 7" fill="none" stroke="rgb(76 141 255)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="19" cy="7" r="1.6" fill="rgb(76 141 255)" />
    </svg>
  );
}
