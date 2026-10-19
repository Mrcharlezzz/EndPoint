/** Shapes aligned with docs/artifacts/design-docs/presentation_api.md (§6). */

export type Platform = "moodle" | "teams" | "telegram";

export type AssignmentStatus = "open" | "closed" | "submitted";

export type PresentationLink = {
  url: string;
  label: string | null;
};

export type PresentationAttachment = {
  filename: string;
  mime_type: string | null;
  url: string | null;
};

export type CourseSummary = {
  id: string;
  name: string;
  code: string | null;
};

export type CourseListItem = {
  id: string;
  name: string;
  code: string | null;
  major: string | null;
  semester: string | null;
};

export type UserCourseSelectionItem = {
  course_id: string;
  name: string;
  code: string | null;
  major: string | null;
  semester: string | null;
};

export type PresentationUser = {
  id: string;
  email: string;
  display_name: string;
  created_at: string;
};

/** `POST /api/v1/auth/login/` request body (design doc §6.1). */
export type LoginRequest = {
  ms_access_token: string;
};

export type LoginResponse = {
  token: string;
  expires_at: string;
  user: Pick<PresentationUser, "id" | "email" | "display_name">;
};

export type ResultsWrapper<T> = {
  results: T[];
};

export type PresentationAssignment = {
  id: string;
  title: string | null;
  body: string | null;
  author: string | null;
  course: CourseSummary;
  deadline: string | null;
  submission_format: string | null;
  max_points: number | null;
  points: number | null;
  status: AssignmentStatus | null;
  platform: Platform;
  source_url: string | null;
  links: PresentationLink[];
  attachments: PresentationAttachment[];
  created_at: string;
};

export type PresentationAnnouncement = {
  id: string;
  title: string | null;
  body: string | null;
  author: string | null;
  platform: Platform;
  source_url: string | null;
  links: PresentationLink[];
  attachments: PresentationAttachment[];
  created_at: string;
};

export type AnnouncementCourseGroup = {
  course: CourseSummary;
  announcements: PresentationAnnouncement[];
};

export type AnnouncementsListResponse = ResultsWrapper<AnnouncementCourseGroup>;

/** `GET /api/v1/announcements/{id}/` — item plus top-level `course`. */
export type AnnouncementDetail = PresentationAnnouncement & {
  course: CourseSummary;
};

export type PresentationEvent = {
  id: string;
  title: string | null;
  body: string | null;
  author: string | null;
  course: CourseSummary;
  start_time: string | null;
  end_time: string | null;
  location: string | null;
  platform: Platform;
  source_url: string | null;
  links: PresentationLink[];
  attachments: PresentationAttachment[];
  created_at: string;
};

export type EventsListResponse = ResultsWrapper<PresentationEvent>;
