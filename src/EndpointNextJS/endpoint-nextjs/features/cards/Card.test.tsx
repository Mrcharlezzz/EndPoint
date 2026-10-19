import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Assignment } from "@/domain/entities";

const markMutate = vi.hoisted(() => vi.fn());
const mockUseSession = vi.hoisted(() =>
  vi.fn(() => ({ isAuthed: true, isResolved: true })),
);

vi.mock("@/lib/queries/useSession", () => ({
  useSession: () => mockUseSession(),
}));

vi.mock("@/lib/queries/useReadState", () => ({
  useMarkRead: () => ({ mutate: markMutate }),
}));

vi.mock("@/features/analytics/events", () => ({
  track: vi.fn(),
}));

import { Card } from "./Card";

const baseCourse = {
  id: "course-1",
  code: "CS 101",
  name: "Intro",
  major: null as string | null,
  semester: null as string | null,
};

const assignmentItem: Assignment = {
  type: "assignment",
  id: "item-1",
  title: "Problem set 1",
  body: "Read chapter 2.",
  author: "TA",
  course: baseCourse,
  platform: "moodle",
  sourceUrl: null,
  publishedAt: "2026-01-15T12:00:00.000Z",
  links: [],
  attachments: [],
  deadline: "2026-02-01T23:59:59.000Z",
  submissionFormat: "file_upload",
};

describe("Card (unit, hooks mocked)", () => {
  beforeEach(() => {
    markMutate.mockClear();
    mockUseSession.mockReturnValue({ isAuthed: true, isResolved: true });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows unread affordance when authed and unread", () => {
    render(<Card item={assignmentItem} isRead={false} />);
    expect(screen.getByLabelText("Unread")).toBeInTheDocument();
  });

  it("hides unread affordance when read", () => {
    render(<Card item={assignmentItem} isRead={true} />);
    expect(screen.queryByLabelText("Unread")).not.toBeInTheDocument();
  });

  it("does not mark read when anonymous", () => {
    mockUseSession.mockReturnValue({ isAuthed: false, isResolved: true });
    const { container } = render(<Card item={assignmentItem} isRead={false} />);
    fireEvent.click(container.querySelector("article.card")!);
    expect(markMutate).not.toHaveBeenCalled();
  });

  it("marks read on card click when authed and unread", () => {
    const { container } = render(<Card item={assignmentItem} isRead={false} />);
    const article = container.querySelector("article.card");
    expect(article).toBeTruthy();
    fireEvent.click(article!);
    expect(markMutate).toHaveBeenCalledWith(["item-1"]);
  });
});
