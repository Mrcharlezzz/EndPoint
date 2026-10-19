import type { Assignment, Attachment, Link } from "@/domain/entities";

import type { ApiAssignment, ApiAttachment, ApiLink } from "@/lib/api/types";

import { toCourse } from "./course";

function toLink(api: ApiLink): Link {
  return { url: api.url, label: api.label };
}

function toAttachment(api: ApiAttachment): Attachment {
  return { filename: api.filename, mimeType: api.mime_type, url: api.url };
}

export function toAssignment(api: ApiAssignment): Assignment {
  return {
    type: "assignment",
    id: api.id,
    title: api.title,
    body: api.body,
    author: api.author,
    course: toCourse(api.course),
    platform: api.platform,
    sourceUrl: api.source_url,
    publishedAt: api.created_at,
    links: api.links.map(toLink),
    attachments: api.attachments.map(toAttachment),
    deadline: api.deadline,
    submissionFormat: api.submission_format,
  };
}
