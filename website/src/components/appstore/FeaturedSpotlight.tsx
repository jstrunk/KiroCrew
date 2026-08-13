/**
 * FeaturedSpotlight — one editorial card in the featured list at the top of Discover.
 *
 * Layout: artwork across the top, then editorial copy, then one row per app.
 * Art and text occupy SEPARATE planes rather than being composited, because
 * artwork arrives from a curator with no guarantee about where its bright or
 * busy region falls — title-over-image would be unreadable on some pictures and
 * nothing in the pipeline could catch it.
 *
 * TWO TYPES, ONE SHELL.
 *  - `app`: one featured app. Its own name is the heading, and the whole card
 *    opens it.
 *  - `collection`: several apps under a curator's theme. The theme is the
 *    heading, and the card itself is NOT clickable — there is no collection
 *    detail page to open, so the rows are the only targets. A card that looked
 *    clickable and did nothing would be worse than one that does not.
 *
 * Every app renders a row with its OWN install control. A member the reader
 * cannot act on is just a picture of an app, and the collection card exists to
 * be acted on.
 */
import { BadgeCheck, Check, Download, Package, Power } from 'lucide-react'
import { Btn } from '../ui'
import Clickable from '../Clickable'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'
import { categoryFor } from './categories'
import { useHeroArt } from './useHeroArt'
import { useEditorialArt, type EditorialArtwork } from './useEditorialArt'
import { sourceLabel, isVerified, type RegistryApp } from './types'
import { appDisplayName, appDescription } from './appManifest'

import { i18nT } from '../../i18n/t'

/**
 * One app inside a featured card: icon, name, a secondary line, and the control
 * that acts on it.
 *
 * The control is per-row rather than per-card. A collection whose card carried a
 * single Get button could only ever install one member, and which one would be
 * an accident of ordering.
 */
function FeaturedAppRow({
  app,
  secondary,
  busy,
  onOpen,
  onGet,
  onEnable,
}: {
  app: RegistryApp
  /** The line under the name. The app's description, or its provenance meta. */
  secondary: string
  busy?: boolean
  onOpen: (e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: () => void
  onEnable: () => void
}) {
  // A built-in that is installed but disabled needs enabling, not installing:
  // the bytes are already present, so offering "Get" would be a no-op.
  const hiddenBuiltin = app.origin === 'builtin' && app.installed && !app.enabled

  return (
    <div className="flex items-center gap-3 py-2.5 min-w-0">
      <Clickable
        aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(app) })}
        className="flex items-center gap-3 flex-1 min-w-0 rounded-[10px] focus-ring cursor-pointer"
        onClick={onOpen}
      >
        <AppIcon icon={app.icon} iconUrl={app.iconUrl} iconUrlDark={app.iconUrlDark} size={38} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 min-w-0">
            {isVerified(app) && (
              <BadgeCheck size={13} className="text-accent shrink-0" aria-label={i18nT('components.appstore.featuredSpotlight.verified_publisher')}>
                <title>{i18nT('components.appstore.featuredSpotlight.verified_publisher_first_party')}</title>
              </BadgeCheck>
            )}
            <span className="text-[13.5px] font-semibold text-text-strong truncate">{appDisplayName(app)}</span>
          </div>
          <p className="text-[12px] text-muted truncate" title={secondary}>{secondary}</p>
        </div>
      </Clickable>
      <div
        className="shrink-0"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
        role="presentation"
      >
        {hiddenBuiltin ? (
          <Btn primary className="rounded-full px-3.5 py-1 font-semibold" disabled={busy} onClick={onEnable}>
            <Power size={13} /> {i18nT('components.appstore.featuredSpotlight.enable')}
          </Btn>
        ) : app.installed ? (
          <span className="inline-flex items-center gap-1.5 text-[12.5px] text-muted whitespace-nowrap"><Check size={13} /> {i18nT('components.appstore.featuredSpotlight.installed')}</span>
        ) : (
          <Btn primary className="rounded-full px-3.5 py-1 font-semibold" disabled={busy} onClick={onGet}>
            <Download size={13} /> {i18nT('components.appstore.featuredSpotlight.get')}
          </Btn>
        )}
      </div>
    </div>
  )
}

export default function FeaturedSpotlight({
  type,
  apps,
  title,
  blurb,
  artwork,
  onOpenApp,
  onGet,
  onEnable,
  busyName,
}: {
  /** Which shape this is. `app` carries one entry in `apps`; `collection` two or more. */
  type: 'app' | 'collection'
  /** Every app in the placement, in the curator's order. Never empty. */
  apps: RegistryApp[]
  /** The curator's theme. Present for a collection, absent for a single app. */
  title?: string
  /** Curator copy, preferred over the app's own description when present. */
  blurb?: string
  artwork?: EditorialArtwork | null
  onOpenApp: (name: string, e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: (name: string) => void
  onEnable: (name: string) => void
  /** Name of the app with an action in flight, so only its own control disables. */
  busyName?: string | null
}) {
  // The lead app anchors the fallback art and the single-app heading. For a
  // collection it is only the art fallback: the heading is the curator's theme,
  // and no member is promoted above the others.
  const lead = apps[0]
  const isCollection = type === 'collection'
  const hero = useHeroArt(lead)
  const editorial = useEditorialArt(artwork)
  // Editorial artwork wins: this is an editorial placement, so the curator's
  // picture is the point of it. The app's own hero is the fallback it always was.
  const artSrc = editorial.src || hero.src
  const onArtError = editorial.src ? editorial.onError : hero.onError

  const heading = isCollection ? title : appDisplayName(lead)
  const sub = blurb || (isCollection ? '' : appDescription(lead))

  const art = (
    <div
      /* A fixed BAND height, not an aspect ratio. Artwork is authored 16:9, but
         this card is ~1000px wide on a desktop dashboard, where 16:9 would be
         ~560px of picture and push every app row below the fold -- the rows are
         the point of the card. `object-cover` crops the 16:9 source into the
         band, which is why the schema treats a ratio mismatch as a warning
         rather than a refusal. */
      className="relative h-[190px] md:h-[220px] overflow-hidden grid place-items-center"
      style={artSrc ? { background: 'var(--bg-elevated)' } : { background: gradientFor(lead.name) }}
    >
      {artSrc ? (
        <img
          src={artSrc}
          alt={editorial.src ? editorial.alt : ''}
          className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
          onError={onArtError}
        />
      ) : (
        <div className="w-[92px] h-[92px] rounded-3xl bg-white/15 border border-white/25 backdrop-blur-sm grid place-items-center text-white">
          {(lead.iconUrl || lead.iconUrlDark || lead.icon) ? <AppIcon icon={lead.icon} iconUrl={lead.iconUrl} iconUrlDark={lead.iconUrlDark} size={56} /> : <Package size={44} />}
        </div>
      )}
    </div>
  )

  const body = (
    <div className="px-5 pt-4 pb-2">
      <span className="text-[11px] font-bold tracking-[.14em] text-accent">
        {isCollection
          ? i18nT('components.appstore.featuredSpotlight.collection')
          : i18nT('components.appstore.featuredSpotlight.featured')}
      </span>
      <h2 className="text-[23px] leading-[1.2] font-bold text-text-strong tracking-tight mt-1.5">{heading}</h2>
      {sub && <p className="text-[14px] text-muted line-clamp-2 mt-1" title={sub}>{sub}</p>}
      <div className="mt-2.5 divide-y divide-border">
        {apps.map(a => (
          <FeaturedAppRow
            key={a.name}
            app={a}
            /* A collection row describes the app, since the card's copy already
               carries the theme. A single-app card has already shown the
               description above, so its row carries provenance instead. */
            secondary={
              isCollection
                ? appDescription(a)
                : `${a.author} · ${categoryFor(a.tags)} · ${i18nT('components.appstore.featuredSpotlight.v')}${a.installedVersion || a.version} · ${sourceLabel(a)}`
            }
            busy={busyName === a.name}
            onOpen={e => onOpenApp(a.name, e)}
            onGet={() => onGet(a.name)}
            onEnable={() => onEnable(a.name)}
          />
        ))}
      </div>
    </div>
  )

  const shell = 'border border-border rounded-[20px] overflow-hidden bg-card mb-3.5 group'

  // A collection has nothing to open, so the card is a plain container and its
  // rows carry every interaction. A single-app card opens that app, which is
  // what a reader expects from tapping a picture of one app.
  if (isCollection) {
    return <div className={shell}>{art}{body}</div>
  }
  return (
    <Clickable
      aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(lead) })}
      className={`${shell} cursor-pointer hover:border-border-strong transition-colors focus-ring`}
      onClick={e => onOpenApp(lead.name, e)}
    >
      {art}{body}
    </Clickable>
  )
}
