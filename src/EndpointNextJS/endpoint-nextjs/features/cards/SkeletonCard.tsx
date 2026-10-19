export function SkeletonCard() {
  return (
    <div className="skeleton-card" aria-hidden>
      <div className="skeleton-line" style={{ width: "30%", height: 10 }} />
      <div className="skeleton-line" style={{ width: "70%", height: 16 }} />
      <div className="skeleton-line" style={{ width: "100%" }} />
      <div className="skeleton-line" style={{ width: "85%" }} />
      <div className="skeleton-line" style={{ width: "40%", marginTop: 16 }} />
    </div>
  );
}

export function SkeletonStream({ count = 4 }: { count?: number }) {
  return (
    <div className="card-stream" aria-busy>
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  );
}
