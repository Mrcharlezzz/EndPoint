/**
 * DTO shapes returned by the presentation-api. Mirrors the wire format
 * (snake_case, ISO timestamps as strings). UI code must not import these
 * directly — go through lib/mappers/ which produces domain types.
 */

export type AuthApiUser = {
  id: string;
  email: string;
  display_name: string;
  created_at: string;
};

export type AuthFinalizeResponse = {
  user: AuthApiUser;
  expires_at: string;
};

export type ApiPlatform = "moodle" | "teams" | "telegram";

export type ApiCourse = {
  id: string;
  code: string;
  name: string;
  major: string | null;
  semester: string | null;
};

export type ApiUserCourse = {
  course_id: string;
  code: string;
  name: string;
  major: string | null;
  semester: string | null;
};

export type ApiLink = { url: string; label: string };
export type ApiAttachment = { filename: string; mime_type: string; url: string };

export type ApiAssignment = {
  id: string;
  title: string;
  body: string | null;
  author: string;
  course: { id: string; name: string; code: string };
  deadline: string | null;
  submission_format: string;
  platform: ApiPlatform;
  source_url: string | null;
  links: ApiLink[];
  attachments: ApiAttachment[];
  created_at: string;
};

export type ApiAnnouncement = {
  id: string;
  title: string;
  body: string | null;
  author: string;
  platform: ApiPlatform;
  source_url: string | null;
  links: ApiLink[];
  attachments: ApiAttachment[];
  created_at: string;
};

export type ApiAnnouncementGroup = {
  course: { id: string; name: string; code: string };
  announcements: ApiAnnouncement[];
};

export type ApiEvent = {
  id: string;
  title: string;
  body: string | null;
  author: string;
  course: { id: string; name: string; code: string };
  start_time: string | null;
  end_time: string | null;
  location: string;
  platform: ApiPlatform;
  source_url: string | null;
  links: ApiLink[];
  attachments: ApiAttachment[];
  created_at: string;
};

export type ApiList<T> = { results: T[] };

export type ApiConnectionStatus = "connected" | "expired" | "paused";

export type ApiConnection = {
  platform: "moodle" | "teams" | "telegram";
  status: ApiConnectionStatus;
  last_sync_at: string | null;
  expired_at: string | null;
};

export type ApiPreferences = {
  landing?: "assignments" | "announcements";
  theme?: "light" | "dark" | "system";
  density?: "comfortable" | "standard" | "compact";
  accent?: "blue" | "teal" | "slate";
  timeFmt?: "12h" | "24h";
};
