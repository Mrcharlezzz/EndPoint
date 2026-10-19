import type { Announcement, Attachment, Link } from "@/domain/entities";

import type { ApiAnnouncementGroup, ApiAttachment, ApiLink } from "@/lib/api/types";

import { toCourse } from "./course";

function toLink(api: ApiLink): Link {
  return { url: api.url, label: api.label };
}

function toAttachment(api: ApiAttachment): Attachment {
  return { filename: api.filename, mimeType: api.mime_type, url: api.url };
}

/**
 * The list endpoint groups announcements by course. We flatten so the UI
 * can sort them alongside events on a single feed by published date —
 * matching the announcements dashboard mental model in the design spec.
 */
export function toAnnouncementsFlat(groups: ApiAnnouncementGroup[]): Announcement[] {
  const flat: Announcement[] = [];
  for (const group of groups) {
    const course = toCourse(group.course);
    for (const a of group.announcements) {
      flat.push({
        type: "announcement",
        id: a.id,
        title: a.title,
        body: a.body,
        author: a.author,
        course,
        platform: a.platform,
        sourceUrl: a.source_url,
        publishedAt: a.created_at,
        links: a.links.map(toLink),
        attachments: a.attachments.map(toAttachment),
      });
    }
  }
  return flat;
}
