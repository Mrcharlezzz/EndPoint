export default function RootLoading() {
  return (
    <main
      style={{
        padding: "2rem",
        maxWidth: "640px",
        margin: "0 auto",
        color: "var(--text-faint)",
      }}
      aria-busy="true"
    >
      Loading…
    </main>
  );
}
