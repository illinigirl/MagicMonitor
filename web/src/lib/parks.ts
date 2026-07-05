/**
 * Park metadata — display names, accent colors, sort order.
 * Single source of truth for the dashboard's park-level UI.
 *
 * Park keys match the Lambda's PARK_KEYS env var and the DynamoDB
 * `park_key` attribute on RIDE STATE rows.
 */

export type ParkKey =
  | "magic_kingdom"
  | "epcot"
  | "hollywood_studios"
  | "animal_kingdom";

export interface Park {
  key: ParkKey;
  name: string;
  shortName: string;
  /** OKLCH var name from globals.css — used as a per-page accent */
  accentVar: string;
  /** Tagline shown on the landing-page card */
  tagline: string;
}

export const PARKS: Park[] = [
  {
    key: "magic_kingdom",
    name: "Magic Kingdom",
    shortName: "MK",
    accentVar: "--park-magic-kingdom",
    tagline: "Castle, mountains, mouse-eared everything.",
  },
  {
    key: "epcot",
    name: "EPCOT",
    shortName: "EP",
    accentVar: "--park-epcot",
    tagline: "That ball, the World Showcase, Cosmic Rewind.",
  },
  {
    key: "hollywood_studios",
    name: "Hollywood Studios",
    shortName: "HS",
    accentVar: "--park-hollywood-studios",
    tagline: "Tower drops, Star Wars, a runaway railway.",
  },
  {
    key: "animal_kingdom",
    name: "Animal Kingdom",
    shortName: "AK",
    accentVar: "--park-animal-kingdom",
    tagline: "Safaris, Pandora, and that very big tree.",
  },
];

export function findPark(key: string): Park | undefined {
  return PARKS.find((p) => p.key === key);
}
