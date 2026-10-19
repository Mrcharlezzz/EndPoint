/**
 * Course domain type + stable course-tint hashing.
 *
 * The 8-hue palette comes from the design spec: calm, restrained colors used
 * only as a 2px overline accent next to the course code. The mapping is a
 * deterministic hash of the course id so the same course always picks up
 * the same hue across reloads and devices.
 */

export type Course = {
  id: string;
  code: string;
  name: string;
  major: string | null;
  semester: string | null;
};

const COURSE_TINTS: ReadonlyArray<string> = [
  "oklch(0.62 0.16 260)", // blue
  "oklch(0.62 0.13 195)", // teal
  "oklch(0.62 0.14 155)", // green
  "oklch(0.65 0.13 75)", // amber
  "oklch(0.6 0.16 25)", // rose
  "oklch(0.62 0.16 305)", // violet
  "oklch(0.6 0.13 230)", // sky
  "oklch(0.6 0.13 105)", // olive
];

function hashCourseId(id: string): number {
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 31 + id.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

export function tintFor(courseId: string): string {
  return COURSE_TINTS[hashCourseId(courseId) % COURSE_TINTS.length] as string;
}
