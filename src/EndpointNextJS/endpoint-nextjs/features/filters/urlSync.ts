"use client";

import { useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import type { Item, Platform } from "@/domain/entities";

import { useFiltersStore } from "./store";

const PLATFORMS: ReadonlyArray<Platform> = ["moodle", "teams", "telegram"];
const TYPES: ReadonlyArray<Item["type"]> = ["assignment", "announcement", "event"];

function parseList(raw: string | null): string[] {
  return raw ? raw.split(",").filter(Boolean) : [];
}

function parsePlatform(raw: string | null): Platform | null {
  return PLATFORMS.includes(raw as Platform) ? (raw as Platform) : null;
}

function parseType(raw: string | null): Item["type"] | null {
  return TYPES.includes(raw as Item["type"]) ? (raw as Item["type"]) : null;
}

/**
 * Bidirectional URL ↔ store sync. Reads `?course=&platform=&type=` into
 * the store on mount + path change; writes the store back into the URL
 * on store changes (replaceState, no history pollution).
 */
export function useFilterUrlSync(): void {
  const router = useRouter();
  const searchParams = useSearchParams();
  const setCourses = useFiltersStore((s) => s.setCourses);
  const setPlatform = useFiltersStore((s) => s.setPlatform);
  const setType = useFiltersStore((s) => s.setType);
  const courses = useFiltersStore((s) => s.courses);
  const platform = useFiltersStore((s) => s.platform);
  const type = useFiltersStore((s) => s.type);
  const hydratedRef = useRef(false);

  // URL → store, once on mount.
  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    const courseRaw = searchParams.get("course");
    const platformRaw = searchParams.get("platform");
    const typeRaw = searchParams.get("type");
    setCourses(parseList(courseRaw));
    setPlatform(parsePlatform(platformRaw));
    setType(parseType(typeRaw));
  }, [searchParams, setCourses, setPlatform, setType]);

  // Store → URL, on every relevant change.
  useEffect(() => {
    if (!hydratedRef.current) return;
    const params = new URLSearchParams(window.location.search);
    if (courses.size > 0) params.set("course", Array.from(courses).join(","));
    else params.delete("course");
    if (platform) params.set("platform", platform);
    else params.delete("platform");
    if (type) params.set("type", type);
    else params.delete("type");
    const qs = params.toString();
    const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    router.replace(url, { scroll: false });
  }, [courses, platform, type, router]);
}
