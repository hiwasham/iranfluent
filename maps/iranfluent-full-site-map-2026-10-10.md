# IranFluent — Full Two-Site Technical Map

Read-only census of the entire IranFluent property, verified live via WP REST on
2026-10-10. Companion to the strategy doc (gbrain page 2132) and `PROJECT.md`.

Methodology: WP REST reads with `Accept-Encoding: identity` (brotli decode bug),
`X-WP-Total` headers for counts, FluentCRM per-entity counts via literal
`tags[]=<id>` / `lists[]=<id>` query form (scalar form is ignored → false zeros).

> Mirrored to REMOTE gbrain (Supabase) 2026-10-10 via the gbrain CLI (the MCP
> server was down, but the CLI reaches Supabase directly). Page slug
> **`maps/iranfluent/2026-10-10-full-site-map`** · revision
> `0f65224d-73d6-4517-8b10-e5a6b0c0e67c` · source `default`.

## Property shape

Two independent WordPress installs, same login, per-site application passwords,
one physical server.

| Install | Role | Builder/theme | Plugins | REST ns |
|---|---|---|---|---|
| `my.iranfluent.com` | LMS + members + CRM + community | LearnDash + BuddyBoss + GamiPress + FluentCRM, LiteSpeed | 178 total / 59 active / 119 inactive | 53 |
| `iranfluent.com` | Marketing / blog | Elementor + Astra + Spectra + ZipWP + RankMath + Presto Player | — (unauth profile) | 34 |

## Infra (SPOF)

- Single server, IP **49.12.129.169** (Hetzner DE, HostDL, h9.hostdl.com).
- Both sites + mail + LMS share this one IP. Same IP is the **Zarinpal payment
  gateway whitelist** → any server/IP migration breaks payments.
- cPanel programmatic login blocked (HTTP 500, Error ID `e1d9fa650044`) → plan/quota,
  disk/inode, CPU/RAM/LVE, PHP/MySQL versions, cron, email, backup config all unreadable.
- Agent work box is `freedom1`, a DIFFERENT machine (not prod).

## my.iranfluent.com — LMS content census

| Entity | Count |
|---|---|
| Courses (sfwd-courses) | 80 |
| Lessons | 1,716 |
| Topics | 2,680 |
| Quizzes | 309 |
| Questions | 1,394 |
| Media (attachments) | 18,005 |
| LearnDash groups | 43 |
| BuddyBoss groups | 48 |
| WP users | 18,160 |
| Taxonomies | 16 |

LearnDash is the ONLY LMS. GamiPress = KEEP (large active gamification CPT
ecosystem). BuddyBoss = community. WooCommerce + Zarinpal = **dormant by design**
(wc/v3 + wc/store both 404 on BOTH sites) — reactivated at commerce launch.

## my.iranfluent.com — FluentCRM

Baseline total **17,100** subscribers. Status: subscribed 13,273 · pending 3,819 ·
unsubscribed 6 · bounced 1 · complained 0.

### Tags — 75 of 100 non-empty (id · slug · count)

217 fluentquiz=161 · 265 english=106 · 242 set_grammar=105 · 234 set_vocab=105 ·
250 gate-entered=103 · 211 decade60=99 · 212 decade70=74 · 231 english-lead=64 ·
255 a2wordscompleted=58 · 258 fluentGame-lead=57 · 213 decade80=56 · 296 oldb1=53 ·
269 set_vocab_B1=53 · 281 a2-grammar-completed=52 · 277 set_grammar_B1=52 · 202 B1=52 ·
201 Learndash-group-b1=51 · 256 set_grammar_A2=38 · 268 set_vocab_A2=35 ·
233 set_secondBrain=35 · 210 decade50=34 · 223 neuro-learning-lead=31 ·
222 AI-chatGPT-lead=31 · 204 setChatGPT=30 · 203 Learndash-Group-Max-chatGPT=30 ·
221 ACCESS-lead=29 · 220 PKM-lead=29 · 224 habit-lead=27 · 229 dopamine-lead=24 ·
230 psy-lead=23 · 228 lifestyle-lead=23 · 275 english-area=22 · 249 gate-opens=21 ·
244 buddyboss-TPRS-group=19 · 239 set_grammar_A1=19 · 225 sleep-lead=19 ·
207 uberdiet=18 · 206 Uberworkout=18 · 236 set_vocab_A1=17 · 276 sounds=16 ·
274 temp-lead=16 · 267 mission-english-speaking=15 · 257 tprs_lead-tag=15 ·
227 body-lead=15 · 232 frommotamem=14 · 209 learner=14 · 235 AIforEnglish=13 ·
226 diet-lead=11 · 218 advancedpronunciation=11 · 292 b1listening=10 · 291 b1reading=10 ·
251 set_TPRS=10 · 214 decade403020=10 · 287 a1-listening=9 · 266 mission-english-reading=7 ·
219 advancedpronunciation_lead=7 · 252 a1wordscompleted=5 · 286 a1-reading=4 ·
261 fluentGame-active=4 · 259 fluentGame-student=4 · 243 fluent_team=4 ·
241 fluent_team_support=3 · 280 a1-grammar-completed=2 · 253 b1wordscompleted=2 ·
245 fluent_team_coursegroupsleader=2 · 238 english_lead_vocab_A1=2 · plus 11 tags at
count 1 (304 diet, 301 english_lead_grammar, 300 english_lead_vocab, 299 english_lead_A1,
298 english_lead, 282 A1-completed, 273 mission-english-listening,
263 fluentGame-minicourse-courseIn, 216 B2Target).

### Lists — 29 of 43 non-empty (id · name · count)

1 main=3901 · 4 لید مگنت تقویم یکساله=1062 · 20 LearndashDefaultList=454 ·
32 پرداختی ها (payments)=238 · 19 WoocommerceDefaultList=201 ·
38 iranfluent.com-webinar=140 · 34 جواب به فرم چند سوالی=132 · 26 ticket=93 ·
42 secondbrain_lead=86 · 3 commentor_list=85 · 22 لیدهای اولویت اول=47 ·
35 لیست ارسال کنندگان فرم مصاحبه=44 · 2 free=41 · 50 english-bundle=38 ·
43 whichCourseform=32 · 30 winter1402=28 · 49 fluent-game-quiz=19 · 46 tprs_lead=15 ·
13 لید جدید=13 · 6 همه اعضا مسترکلاس=8 · 5 مسترکلاس یکساله یکجا=5 ·
29 redundant-contact=4 · 23 تمدید های اولویت اول=3 · 17 ترم دوم=3 ·
14 ترم دوم مسترکلاس=3 · 37 redflag=2 · 33 refunded=2 · 27 ترم1-پاییز1402=1 · 7 کوچ‌ها=1.

## my.iranfluent.com — Nav menus (12, id · item count)

BuddyPanel(8790)=18 · PrimaryMenu(8736)=16 · FluentProfileDropDownMenu(9160)=13 ·
Buddyboss English(8973)=5 · Dashboard Widgets Suite(9007)=3 · fluent-bundle-menu(9216)=3 ·
English Courses(9211)=2 · focusedLearnDashMenu(9161)=2 · قوانین و حریم شخصی(8808)=2 ·
Persian-logged-out-mobile(9085)=1 · ReadyLaunch(9219)=0 · test(9090)=0.

## my.iranfluent.com — plugin stack

178 total · 59 active · 119 inactive. Core active: LearnDash, FluentCRM, GamiPress
(+ many GamiPress add-ons), BuddyBoss, LiteSpeed Cache. `gamipress-buddyboss-integration`
was removed 2026-10-10 (fatal that took the LMS down). WooCommerce + Zarinpal present
but dormant.

### ⚠️ Tranche-1 target correction

The old dead-plugin set (EDD · LearnPress · Restrict Content Pro) is INVALID — none
installed under real slugs. Only EDD-family trace: one orphaned INACTIVE
`integrate-zarinpal-edd`. **Re-derive the removal set from the live 119 inactive
plugins before acting.** Any removal is a live-prod destructive write → verify what
each touches + explicit confirm, one at a time.

## iranfluent.com — marketing site profile (unauth public REST)

Site name (fa): "یادگیری زبان انگلیسی و مهارتهای دنیای جدید". Pure marketing/blog —
LearnDash sfwd-courses=404, wc/v3=404, wc/store=404 (no LMS, no commerce).

| Entity | Count |
|---|---|
| Posts | 558 |
| Pages | 79 |
| Categories | 27 |
| Tags | 79 |
| Media | 6,956 |
| Public authors | 10 |

### Top categories (of 27, by post count)

157 یادگیری گرامر · 117 درس‌های گرامر · 93 دیکشنری چند لایه · 86 مهارتهای چهارگانه ·
61 خواندن · 51 تلفظ · 41 مکالمه · 37 یادگیری کلمات · 35 متفرقه · 30 شنیدن ·
23 ابزارها و سایتها · 21 آموزش انکی.

### Public authors (10, id · name)

793 ContentM(Fae) · 792 Fluent Support · 794 Tooraj.bastani · 977 امیرحسین ·
2047 امین جباری · 2094 سامان زندیانی · 2045 فائزه (پشتیبان) · 232 فرشته ·
239 میلاد پشت دری · 1 هیوا.

### Post types

post, page, attachment, nav_menu_item, wp_block, wp_template(+part),
wp_global_styles, wp_navigation, wp_font_family, wp_font_face, e-floating-buttons,
elementor_library, pp_video_block (Presto Media Hub), spectra-popup,
rank_math_schema, astra-advanced-hook.

### REST namespaces (34)

elementor-mcp-composer/v1.0.19, elementor-one/v1, elementor/v1, elementor/v1/documents,
elementor-ai/v1, elementor/v1/feedback, astra/v1, astra-addon/v1, astra_addon/v1,
spectra/v1, uag/v1, rankmath/v1 (+setupWizard/an/ai-visibility/status),
presto-player/v1 (+license), wp-smush/v1, objectcache/v1, gtm4wp/v2, zipwp/v1,
zipwp-images/v1, gutenberg-templates/v1, one-onboarding/v1, bsf-core/v1,
nps-survey/v1, wp-toolkit/api, oembed/1.0, mcp, wp/v2, wp-site-health/v1,
wp-block-editor/v1, wp-abilities/v1.

## What this map confirms for strategy

- Stack to standardize on (CEO direction 2026-09-27): **Fluent suite + LearnDash**.
  Both are live and central. GamiPress stays.
- Consolidation (tranches 0–2) before growth (tranche 3). Tranche 1 removal set
  must be re-derived — the old one was wrong.
- Migration is IP-coupled to Zarinpal → tranche 2 (WooCommerce → FluentCart) is the
  riskiest and is parked.
- Marketing site is cleanly separable (no LMS/commerce) — safe to iterate independently.
