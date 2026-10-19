/**
 * Card-shaped domain entities — what the UI consumes after mapping from the
 * presentation-api DTOs. Discriminated by `type` so the Card component can
 * dispatch type-specific meta rows without runtime checks.
 */

import type { Course } from "./course";

export type Platform = "moodle" | "teams" | "telegram";

export type Link = {
  url: string;
  label: string;
};

export type Attachment = {
  filename: string;
  mimeType: string;
  url: string;
};

export type ItemBase = {
  id: string;
  title: string;
  body: string | null;
  author: string;
  course: Course;
  platform: Platform;
  sourceUrl: string | null;
  publishedAt: string;
  links: Link[];
  attachments: Attachment[];
};

export type Assignment = ItemBase & {
  type: "assignment";
  deadline: string | null;
  submissionFormat: string;
};

export type Announcement = ItemBase & {
  type: "announcement";
};

export type Event = ItemBase & {
  type: "event";
  startTime: string | null;
  endTime: string | null;
  location: string;
};

export type Item = Assignment | Announcement | Event;
