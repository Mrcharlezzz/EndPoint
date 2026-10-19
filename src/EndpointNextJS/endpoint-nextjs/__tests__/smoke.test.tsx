import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

function SmokeBanner() {
  return (
    <div>
      <h1>Vitest + RTL</h1>
      <p>UI tests run here.</p>
    </div>
  );
}

describe("Vitest + React Testing Library", () => {
  it("renders copy", () => {
    render(<SmokeBanner />);
    expect(screen.getByRole("heading", { name: "Vitest + RTL" })).toBeInTheDocument();
    expect(screen.getByText("UI tests run here.")).toBeInTheDocument();
  });
});
