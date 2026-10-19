import type { ReactElement } from 'react';

export type IconName =
  | 'dashboard'
  | 'users'
  | 'user'
  | 'sync'
  | 'alert'
  | 'key'
  | 'settings'
  | 'log'
  | 'audit'
  | 'config'
  | 'bell'
  | 'search'
  | 'cmdk'
  | 'close'
  | 'copy'
  | 'external'
  | 'arrow-right'
  | 'chevron-down'
  | 'filter'
  | 'pause'
  | 'stop'
  | 'refresh'
  | 'trending-up'
  | 'trending-down'
  | 'check'
  | 'x-circle'
  | 'clock';

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
}

export function Icon({ name, size = 14, className }: IconProps): ReactElement {
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.5,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    className,
    'aria-hidden': true,
  };
  switch (name) {
    case 'dashboard':
      return (
        <svg {...common}>
          <rect x="2" y="2" width="5" height="5" rx="1" />
          <rect x="9" y="2" width="5" height="5" rx="1" />
          <rect x="2" y="9" width="5" height="5" rx="1" />
          <rect x="9" y="9" width="5" height="5" rx="1" />
        </svg>
      );
    case 'users':
      return (
        <svg {...common}>
          <circle cx="6" cy="5" r="2.5" />
          <path d="M2 13.5c0-2.2 1.8-4 4-4s4 1.8 4 4" />
          <circle cx="11.5" cy="5.5" r="2" />
          <path d="M10.5 9.6c2 .2 3.5 1.9 3.5 3.9" />
        </svg>
      );
    case 'user':
      return (
        <svg {...common}>
          <circle cx="8" cy="5" r="2.8" />
          <path d="M2.5 14c0-2.5 2.4-4.5 5.5-4.5S13.5 11.5 13.5 14" />
        </svg>
      );
    case 'sync':
      return (
        <svg {...common}>
          <path d="M13 3v3h-3" />
          <path d="M3 13v-3h3" />
          <path d="M13 6a5 5 0 0 0-9-1" />
          <path d="M3 10a5 5 0 0 0 9 1" />
        </svg>
      );
    case 'alert':
      return (
        <svg {...common}>
          <path d="M8 1.5L14.5 13h-13L8 1.5z" />
          <path d="M8 6v3.5M8 11.5v.01" />
        </svg>
      );
    case 'key':
      return (
        <svg {...common}>
          <circle cx="5" cy="11" r="2.5" />
          <path d="M7 9.5l6.5-6.5M11 5.5l1.5 1.5" />
        </svg>
      );
    case 'settings':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="2" />
          <path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1" />
        </svg>
      );
    case 'log':
      return (
        <svg {...common}>
          <path d="M3 2h10v12H3z" />
          <path d="M5 5h6M5 8h6M5 11h4" />
        </svg>
      );
    case 'audit':
      return (
        <svg {...common}>
          <path d="M3 1.5h7l3 3V14H3z" />
          <path d="M10 1.5V5h3" />
          <path d="M5.5 9l1.5 1.5L11 7" />
        </svg>
      );
    case 'config':
      return (
        <svg {...common}>
          <path d="M2 4h12M2 8h12M2 12h12" />
          <circle cx="5" cy="4" r="1.2" fill="currentColor" />
          <circle cx="10" cy="8" r="1.2" fill="currentColor" />
          <circle cx="4" cy="12" r="1.2" fill="currentColor" />
        </svg>
      );
    case 'bell':
      return (
        <svg {...common}>
          <path d="M4 6.5a4 4 0 0 1 8 0V10l1 2H3l1-2V6.5z" />
          <path d="M6.5 13.5a1.5 1.5 0 0 0 3 0" />
        </svg>
      );
    case 'search':
      return (
        <svg {...common}>
          <circle cx="7" cy="7" r="4.5" />
          <path d="M10.5 10.5L14 14" />
        </svg>
      );
    case 'cmdk':
      return (
        <svg {...common}>
          <path d="M5 5h6v6H5z" />
          <path d="M2 5h3M11 5h3M2 11h3M11 11h3M5 2v3M11 2v3M5 11v3M11 11v3" />
        </svg>
      );
    case 'close':
      return (
        <svg {...common}>
          <path d="M3 3l10 10M13 3L3 13" />
        </svg>
      );
    case 'copy':
      return (
        <svg {...common}>
          <rect x="5" y="5" width="9" height="9" rx="1" />
          <path d="M2 10V3a1 1 0 0 1 1-1h7" />
        </svg>
      );
    case 'external':
      return (
        <svg {...common}>
          <path d="M6 3H3v10h10v-3" />
          <path d="M10 2h4v4M8 8l6-6" />
        </svg>
      );
    case 'arrow-right':
      return (
        <svg {...common}>
          <path d="M3 8h10M9 4l4 4-4 4" />
        </svg>
      );
    case 'chevron-down':
      return (
        <svg {...common}>
          <path d="M4 6l4 4 4-4" />
        </svg>
      );
    case 'filter':
      return (
        <svg {...common}>
          <path d="M2 3h12l-4.5 6v4L6.5 14.5V9z" />
        </svg>
      );
    case 'pause':
      return (
        <svg {...common}>
          <rect x="4" y="3" width="3" height="10" />
          <rect x="9" y="3" width="3" height="10" />
        </svg>
      );
    case 'stop':
      return (
        <svg {...common}>
          <rect x="3" y="3" width="10" height="10" rx="1" />
        </svg>
      );
    case 'refresh':
      return (
        <svg {...common}>
          <path d="M14 3v3h-3" />
          <path d="M2 13v-3h3" />
          <path d="M14 6a6 6 0 0 0-11-2" />
          <path d="M2 10a6 6 0 0 0 11 2" />
        </svg>
      );
    case 'trending-up':
      return (
        <svg {...common}>
          <path d="M2 11l4-4 3 3 5-5" />
          <path d="M10 5h4v4" />
        </svg>
      );
    case 'trending-down':
      return (
        <svg {...common}>
          <path d="M2 5l4 4 3-3 5 5" />
          <path d="M10 11h4V7" />
        </svg>
      );
    case 'check':
      return (
        <svg {...common}>
          <path d="M3 8l3.5 3.5L13 5" />
        </svg>
      );
    case 'x-circle':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="5.5" />
          <path d="M6 6l4 4M10 6l-4 4" />
        </svg>
      );
    case 'clock':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="5.5" />
          <path d="M8 5v3l2 1.5" />
        </svg>
      );
  }
}
