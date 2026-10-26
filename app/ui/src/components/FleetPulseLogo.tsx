/** Fleet Pulse mark: a heartbeat line over three server nodes. currentColor follows light and dark mode. */
export function FleetPulseLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true" focusable="false">
      <path d="M2.5 11H7l2-5 3.5 9 2-4H21.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
      <rect x={4} y={17.5} width={3.5} height={3.5} rx={1} fill="currentColor" />
      <rect x={10.25} y={17.5} width={3.5} height={3.5} rx={1} fill="currentColor" />
      <rect x={16.5} y={17.5} width={3.5} height={3.5} rx={1} fill="currentColor" />
    </svg>
  );
}
