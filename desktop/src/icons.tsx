/** Quiet 16x16 stroke icons — currentColor, no emoji / CDN. */
import type { ReactElement, ReactNode, SVGProps } from "react";
import type { NavId } from "./types";

type IconProps = SVGProps<SVGSVGElement> & {
  size?: number;
  title?: string;
};

function Base({
  size = 16,
  title,
  children,
  className,
  ...rest
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
}

export function IconOverview(p: IconProps) {
  return (
    <Base {...p}>
      <rect x="2" y="2" width="5" height="5" rx="1" />
      <rect x="9" y="2" width="5" height="5" rx="1" />
      <rect x="2" y="9" width="5" height="5" rx="1" />
      <rect x="9" y="9" width="5" height="5" rx="1" />
    </Base>
  );
}

export function IconWorkbench(p: IconProps) {
  return (
    <Base {...p}>
      <rect x="2" y="2.5" width="12" height="11" rx="1.5" />
      <path d="M6 2.5v11" />
    </Base>
  );
}

export function IconModels(p: IconProps) {
  return (
    <Base {...p}>
      <path d="M8 2.5 13 5.5 8 8.5 3 5.5 8 2.5Z" />
      <path d="M3 8l5 3 5-3" />
      <path d="M3 10.5l5 3 5-3" />
    </Base>
  );
}

export function IconKeys(p: IconProps) {
  return (
    <Base {...p}>
      <circle cx="6" cy="7" r="3.25" />
      <path d="M8.5 9.5 13.5 14.5" />
      <path d="M11.5 12.5h2.5v2" />
    </Base>
  );
}

export function IconAudits(p: IconProps) {
  return (
    <Base {...p}>
      <path d="M4.5 2.5h7A1.5 1.5 0 0 1 13 4v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4A1.5 1.5 0 0 1 4.5 2.5Z" />
      <path d="M5.5 6h5M5.5 8.5h5M5.5 11h3.5" />
    </Base>
  );
}

export function IconMedia(p: IconProps) {
  return (
    <Base {...p}>
      <rect x="2" y="3" width="12" height="10" rx="1.5" />
      <circle cx="5.5" cy="6.5" r="1" />
      <path d="m5 11 2.5-2.5L10 11l1.5-1.5L14 11" />
    </Base>
  );
}

export function IconPlayground(p: IconProps) {
  return (
    <Base {...p}>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M6.75 5.75v4.5L11 8 6.75 5.75Z" fill="currentColor" stroke="none" />
    </Base>
  );
}

export function IconSetup(p: IconProps) {
  return (
    <Base {...p}>
      <rect x="3" y="3.5" width="10" height="9" rx="1.5" />
      <circle cx="8" cy="8" r="1.75" />
      <path d="M8 9.75v1.5" />
    </Base>
  );
}

export function IconSettings(p: IconProps) {
  return (
    <Base {...p}>
      <circle cx="8" cy="8" r="2.25" />
      <path d="M8 2.5v1.5M8 12v1.5M2.5 8H4M12 8h1.5M4.05 4.05l1.06 1.06M10.89 10.89l1.06 1.06M4.05 11.95l1.06-1.06M10.89 5.11l1.06-1.06" />
    </Base>
  );
}

export function IconClose(p: IconProps) {
  return (
    <Base {...p}>
      <path d="M4 4l8 8M12 4 4 12" />
    </Base>
  );
}

export function IconPlay(p: IconProps) {
  return (
    <Base {...p}>
      <path d="M5 3.75v8.5L12.25 8 5 3.75Z" />
    </Base>
  );
}

export function IconStop(p: IconProps) {
  return (
    <Base {...p}>
      <rect x="4.25" y="4.25" width="7.5" height="7.5" rx="1.25" />
    </Base>
  );
}

export function IconRefresh(p: IconProps) {
  return (
    <Base {...p}>
      <path d="M13 8a5 5 0 1 1-1.2-3.3" />
      <path d="M13 3.5V7H9.5" />
    </Base>
  );
}

export function IconOffline(p: IconProps) {
  return (
    <Base {...p}>
      <path d="M3 8h3.5L8 5.5 9.5 10.5 11 8H13" />
      <circle cx="3" cy="8" r="1" fill="currentColor" stroke="none" />
      <circle cx="13" cy="8" r="1" fill="currentColor" stroke="none" />
    </Base>
  );
}

/** Brand mark: abstract bridge / G silhouette on accent tile. */
export function BrandMark({ className }: { className?: string }) {
  return (
    <span className={className ?? "brand-mark"} aria-hidden="true">
      <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
        <path
          d="M4 5.5h5.5a2.5 2.5 0 0 1 0 5H7.5V8.25h2.25"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

const NAV_ICONS: Record<NavId, (p: IconProps) => ReactElement> = {
  overview: IconOverview,
  workbench: IconWorkbench,
  models: IconModels,
  keys: IconKeys,
  audits: IconAudits,
  media: IconMedia,
  playground: IconPlayground,
  setup: IconSetup,
};

export function NavIcon({ id, className }: { id: NavId; className?: string }) {
  const Cmp = NAV_ICONS[id];
  return <Cmp className={className ?? "nav-ico"} size={16} />;
}
