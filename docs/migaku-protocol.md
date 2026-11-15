# Migaku SRS login + sync protocol (working notes)

Source: static analysis of the Migaku browser extension bundle as shipped in
`migaku-safari/Migaku Safari/Migaku Safari Extension/Resources/assets/`
(core version string `"21.35.0-earlyaccess.99"`, commit `4eb7a8090aded2bba9a8e87634fe1ec2392ddaef`).
No network requests were made. Offsets below are character offsets into the
one-line minified files (useful for re-finding the code; they change on every
Migaku release).

Main files:
- `ApiWeb-1aa5ba14.js` (Kotlin/JS "srscore" + Ktor client + Firebase JS SDK 9.19.1) – almost everything.
- `sqljs.worker-72c034e7.js` – sql.js worker: DB import/export, batched pull-apply upserts.
- `util.core-3e485932.js`, `player-store-6301d5da.js`, `CardCreatorPage-8b6be550.js` – front-end card payloads / media upload.

Legend: **[code]** = confirmed from code. **[guess]** = inference, needs a live check.

---

## 0. TL;DR for the Python server

1. Sign in with Firebase Auth (email/password) → Firebase **ID token**.
2. Send `Authorization: Bearer <idToken>` to every Migaku host. Refresh on 401.
3. First sync: `GET https://core-server.migaku.com/server-version?uid=<uid>` (read `X-Server-Version`),
   then `GET https://srs-db-presigned-url-service-api.migaku.com/db-force-sync-download-url` → body is a URL;
   `GET` that URL (no auth) → **gzip-compressed SQLite** file. Gunzip, open with `sqlite3`.
   Store cursor = the `X-Server-Version` read *before* the download.
4. Incremental: `GET https://core-server.migaku.com/pull-sync?serverVersion=<cursor>&deviceId=..&uid=..&applicationEnvironment=..&coreVersion=..`
   → JSON `SyncObject` (maybe gzip). New cursor = response header `X-Server-Version`. Loop while
   `X-Latest-Server-Version` > `X-Server-Version`.
5. Write: `POST https://core-server.migaku.com/push/enqueue?deviceId=..&deviceVersion=<cursor>&uid=..&applicationEnvironment=..&coreVersion=..`
   body = `b"LZ" + u32le(len(json)) + lzo1x(json_utf8)`, headers `Content-Type: application/octet-stream`,
   `X-Content-Encoding: lzo`. 202 = accepted, 409 = pull first, then retry.
6. Never call the force-sync upload/delete/backup endpoints (section 7).

---

## 1. Auth

### 1.1 Firebase project config **[code]**
`ApiWeb` @3192321, class logger `"Core - FirebaseInterface"`:
```
new aG(w??"1:437922001437:web:ae0683b223bbd416814cf4",   // appId
       "AIzaSyDZvwYKYTsQoZkf3oKsfIQ4ykuy2GZAiH8",         // apiKey
       "https://migaku-181d2-default-rtdb.firebaseio.com", // databaseURL
       "G-M9VL1QZN94",                                     // measurementId
       "migaku-181d2.appspot.com",                         // storageBucket
       "migaku-181d2",                                     // projectId
       "437922001437",                                     // messagingSenderId
       "migaku-181d2.firebaseapp.com")                     // authDomain
```
(The other `AIza...` key found in `main-81d59125.js` is YouTube's InnerTube key – unrelated.)

### 1.2 Sign-in methods **[code]**
Bridge method names (ApiWeb @3705711): `Firebase.signInWithEmailAndPassword`,
`Firebase.signInWithGoogleToken`, `Firebase.signInWithFacebookToken`, `Firebase.signInWithAppleToken`,
`Firebase.signInWithCustomToken`, `Firebase.signInOrCreateAndInitializeUserWith{Google,Facebook,Apple}Token`.
Email/password impl @3066423: calls the Firebase JS SDK `signInWithEmailAndPassword` with up to 3 attempts
(log: `"signInWithEmailAndPassword attempt "+n+"/3 for email=..."`).

The SDK internals in the bundle confirm the REST endpoints used under the hood **[code]**:
- `Krn(t,i){return kV(t,"POST","/v1/accounts:signInWithPassword",tP(t,i))}` (@557596), host `apiHost:"identitytoolkit.googleapis.com"` (@623472).
- Token refresh @532048: `grant_type:"refresh_token",refresh_token:i` form-POST to `tokenApiHost` `"securetoken.googleapis.com"` path `"/v1/token"` with `key=${apiKey}`.

Python equivalent (standard Firebase REST) **[guess — standard Firebase, not Migaku-specific]**:
```
POST https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=AIzaSyDZvwYKYTsQoZkf3oKsfIQ4ykuy2GZAiH8
{"email": ..., "password": ..., "returnSecureToken": true}
-> {idToken, refreshToken, expiresIn, localId (= uid)}

POST https://securetoken.googleapis.com/v1/token?key=<apiKey>
Content-Type: application/x-www-form-urlencoded
grant_type=refresh_token&refresh_token=<refreshToken>
-> {id_token, refresh_token, expires_in, user_id}
```
The SDK also sends `X-Client-Version` and `X-Firebase-gmpid: <appId>` (@553885). No App Check is
initialised by Migaku (no `initializeAppCheck`/ReCaptcha provider in the bundle) **[code]**.
Risk **[unknown]**: the browser API key may be HTTP-referrer restricted in Google Cloud; only a live
call shows whether a server-side call is accepted.

### 1.3 Token → Migaku servers **[code]**
There is **no Migaku session/JWT exchange**. Every Migaku HTTP client is a Ktor client with the
Bearer `Auth` plugin whose `loadTokens` is `firebaseInterface.ajz(forceRefresh=false)` (Firebase
`getIdToken`) and `refreshTokens` (on 401) is `ajz(true)`; `sendWithoutRequest = true` so the header
is sent pre-emptively.
- @3214219 `function Jgt(t,i,n){var e=AAn();i.m55(e,B_e(n))}` installs auth into
  `authenticatedPullSyncHttpClient`, `authenticatedTimeoutRetryHttpClient`,
  `authenticatedContentNegotiation...HttpClient` (@3232596).
- Log strings: `"Auth.loadTokens() - attempting to load token (forceRefresh=false)"`,
  `"Auth.refreshTokens() - 401 received, attempting token refresh (forceRefresh=true)"`.
- Header: `"Bearer "+token` into `un().v4a_1` = `"Authorization"` (Ktor `HttpHeaders` table @1472718).

So: `Authorization: Bearer <Firebase ID token>` for core-server, the presigned-url service,
file-sync-worker, user-service and cloud functions alike. The presigned URLs themselves are fetched
with the *unauthenticated* client (`Yn().iel_1`).

### 1.4 Post-login calls (not needed for sync) **[code]**
- `POST https://us-central1-migaku-181d2.cloudfunctions.net/app/auth/user-logged-in`, JSON
  `{"applicationEnvironment": "..."}` – analytics ("trackUserLoggedIn") @3157412.
- `POST https://user-service.migaku.com/app/user/initialize` (body `InitializeUserRequest`:
  interfaceLang, signUpPlatform, signUpAuthProvider, planId?, ...) – only for new sign-ups @3121137.
- Firestore doc `users/<uid>/sync/syncInfo` is only a change *listener*: on snapshot the client
  runs a pull (`"Database change flow received - pull notification"` @3198052). A server can poll instead.

### 1.5 Common query values **[code]**
- `uid` = Firebase `currentUser.uid` (`Uti()` @3931446, `"unknown"` if signed out).
- `deviceId` = 32 random hex chars, persisted under key `"core.deviceId"` (DeviceMetaReporter @`s(oKt).i9`).
- `applicationEnvironment` = `"extension"` | `"web"` (study.migaku.com) | `"website"` (`AB()` @4763527). **[guess]** use `"extension"` or `"web"`.
- `coreVersion` = `"21.35.0-earlyaccess.99"` (`No().e56()` @2829722). Server may gate on it **[unknown]**.

---

## 2. Incremental sync

Syncer class (logger `"Core - Syncer"`, ctor @~4032000):
`SYNC_SERVER_HOST` default `https://core-server.migaku.com`,
`FORCE_SYNC_HOST` default `https://srs-db-presigned-url-service-api.migaku.com`.

### 2.1 Pull **[code]** (@3957389)
```
"Pull: GET "+host+"/pull-sync?serverVersion="+v+"&deviceId="+d+"&uid="+g+"&applicationEnvironment="+bw()+"&coreVersion="+No().e56()
```
- Method GET (`Mi().q4e_1` = `"GET"`), client `authenticatedPullSyncHttpClient` (Bearer; 15 s connect, 60 s socket/request, 3 retries).
- Query params: `serverVersion` (= local `LocalSync.lastSyncedServerVersion`), `deviceId`, `uid`, `applicationEnvironment`, `coreVersion`.
- Non-browser builds add `Accept-Encoding: gzip`. Body is gunzipped if `Content-Encoding: gzip` **or** bytes start with `1f 8b`.
- Non-200 → error (`"Pull: failed "+status`). 5xx/408/429 are retryable (`gWi`).
- Body = UTF-8 JSON decoded as `io.migaku.srscore.SyncObject` with a kotlinx `Json { ignoreUnknownKeys = true; encodeDefaults = true }` (`Jke` @3951xxx) **[code; flag mapping inferred from JsonBuilder field order]**.
- Response headers: `X-Server-Version` (page max) and `X-Latest-Server-Version`.
  New cursor := `X-Server-Version` (not the payload max). If `X-Latest-Server-Version` > `X-Server-Version`:
  `"Pull: more pages available (pageMax=..., latest=...), looping"`.
- If `lastSyncedServerVersion == 0` and the initial force pull was never attempted, the client
  **skips** incremental pull (`"Pull: lastSyncedServerVersion=0 and initial pull never attempted; skipping incremental pull"`)
  and does a force pull (section 3). Whether `serverVersion=0` would return the whole collection is **[unknown]**.

Apply rule (sqljs worker / SQLDelight upserts) **[code]**: server rows are upserted only if newer:
`... ON CONFLICT(id) DO UPDATE SET ... serverVersion = excluded.serverVersion, isPendingEnqueue = 0, isPendingApply = 0 WHERE excluded.serverVersion > card.serverVersion`
(same `WHERE ? > <table>.serverVersion` guard on card_type, config, deck, keyValue, learningMaterial,
library, review, reviewHistory, vacation, wordHistory).

### 2.2 SyncObject JSON **[code]** (@3907228)
```
io.migaku.srscore.SyncObject {
  decks: [SerializableDeck]            = []
  cardTypes: [SerializableCardType]    = []
  cards: [SerializableCard]            = []
  cardWordRelations: [SerializableCardWordRelation] = []
  vacations: [SerializableVacation]    = []
  reviews: [SerializableReview]        = []
  words: [SerializableWord]            = []      // WordList table
  config: SerializableConfig | null    = null    // push always sends null
  keyValue: [SerializableKeyValue]     = []
  learningMaterials: [SerializableLearningMaterial] = []
  lessons: [SerializableLesson]        = []
  reviewHistory: [SerializableReviewHistory] = []
  wordHistory: [SerializableWordHistory] = []
  libraryItems: [SerializableLibraryItem] = []
}
```
Field lists/types of the row objects are in section 4 (they mirror the SQLite columns; Long → JSON
integer, booleans as JSON `true/false` where the type says bool, even though SQLite stores 0/1).

### 2.3 Push **[code]** (@3966448)
```
"/push/enqueue?deviceId="+id+"&deviceVersion="+lastSyncedServerVersion+"&uid="+uid+"&applicationEnvironment="+bw()+"&coreVersion="+No().e56()
```
1. Collect all rows `WHERE isPendingEnqueue = 1` from every table (`tii` builds
   `SyncObject(decks, cardTypes, cards, cardWordRelations, vacations, reviews, words, config=null, keyValue, learningMaterials, lessons, reviewHistory, wordHistory, libraryItems)`).
2. Split into batches of max 1000 rows (`"maxRowsPerBatch=1000"`).
3. Per batch: JSON-encode, strip lone UTF-16 surrogates, UTF-8 encode, LZO-compress:
   `gt=iOi(nt,oOi())` where `oOi()` = korlibs `LZO` with header type **SHORT** (`cOi()=sOi`) →
   wire format `"LZ"` (0x4C 0x5A) + uint32 **little-endian** uncompressed length + raw LZO1X stream
   (aircompressor-style `LzoRawCompressor`, ends with `11 00 00`). Python: `b"LZ"+struct.pack("<I",len(raw))+lzo.compress(raw,1,False)` **[guess: python-lzo output compatible; LZO1X is standard]**.
4. `POST` (client `authenticatedTimeoutRetryHttpClient`, Bearer), headers
   `Content-Type: application/octet-stream`, `X-Content-Encoding: lzo`.
5. Response:
   - `202 Accepted` → body `EnqueueResponse {enqueued: bool, messageId?: str, orderingKey?: str, receivedAt?: long}`;
     client then marks rows `UPDATE card SET serverMod = 0, isPendingEnqueue = 0, isPendingApply = 1 WHERE id = ? AND mod <= ? AND del = ?`.
   - `409 Conflict` → `"Push sync: Conflict (...) Will pull then retry."` – pull, then retry (bounded retries).
   - other → error.
6. The server applies the queue asynchronously (messageId/orderingKey ⇒ Pub/Sub style); the rows come
   back later via pull with a real `serverVersion` **[guess on mechanism; 202 + messageId is code]**.

Local-dirty convention used for every write **[code]**: `mod = <now ms>, serverMod = -1, isPendingEnqueue = 1, isPendingApply = 0`.
So pushed rows carry `serverMod: -1` and `serverVersion` = whatever was last seen (0 for new rows).
Deletions are soft: `del = <now ms>` (CardMgr delete passes `lfk().pf4()` = epoch ms); `del = 0` = live.

### 2.4 Other read-only endpoints on core-server **[code]**
- `GET /server-version?uid=<uid>` → header `X-Server-Version` (authoritative current version) @4002869.
- `GET /learning-material-meta-information` → SyncObject JSON (course metadata).
- `GET /learning-material/<id>?timestamp=<ts>` → course SyncObject.

---

## 3. Force pull / full DB download **[code]**

`forcePullSync` (@~4002000, `fii`):
1. `GET {SYNC}/server-version?uid=<uid>` → remember `X-Server-Version` = `V`.
2. `GET https://srs-db-presigned-url-service-api.migaku.com/db-force-sync-download-url` (Bearer) →
   **200 body is a plain-text URL** (presigned; the client links GCS consoles elsewhere, so likely Google Cloud Storage **[guess]**).
   Non-200: `"Failed to get download url, response: "+status+", cause: "+body`; a 404 is treated as
   "remote DB missing … remote will be created on first push".
3. `GET <url>` with the unauthenticated client, 600 s timeout; empty body → `"Downloaded database is empty"`.
4. Bytes are **gzip-compressed SQLite** (`srs.db`): the importer either stores them as-is in
   IndexedDB (which always holds gzip – worker reads with `DecompressionStream("gzip")`) or calls
   `LY(bytes, CS())` where `CS()` is korlibs `GZIP` (`toString()=="GZIP"`), then the sql.js worker
   `import_preserving_dictionaries`: `ATTACH` the file and for every table except
   `langResourceInfo/langResourceEntry/langVocabularyEntry` does
   `DELETE FROM main.T; INSERT INTO main.T (common cols) SELECT common cols FROM import_source.T`.
   ⇒ the server DB has the **same schema** as section 4.
5. Set `LocalSync.lastSyncedServerVersion = V` (the value read *before* download, "Firestore-authoritative").

Python: `sqlite3.connect()` on `gzip.decompress(body)` written to disk. sql.js is SQLite 3 (`"SQLite format 3\0"` magic checked elsewhere).

---

## 4. Data model (SQLite, from `CREATE TABLE` @2536345) **[code]**

All synced tables share: `mod INTEGER` (local edit time, epoch ms), `serverMod INTEGER` (-1 = dirty),
`del INTEGER` (0 or deletion epoch ms), `serverVersion INTEGER DEFAULT 0` (indexed; server cursor),
`isPendingEnqueue INTEGER`, `isPendingApply INTEGER`. Seeded (course) rows add `seedMod`, `seedDel`, JSON `isSeed`.
IDs are epoch-ms based, strictly increasing per device (`kf1(): t=now; if t<=last then last+1`).

**card**
```
id PK, mod, serverMod, del, deckId, cardTypeId, created (ms),
primaryField TEXT, secondaryField TEXT, fields TEXT, words TEXT,
due INT (day number, 0 = new), balancedDue INT, interval REAL, factor REAL, lastReview INT (day number),
reviewCount, passCount, failCount, lapseCount, pos, lessonId NULL, seedMod, notes TEXT, seedDel,
suspended DEFAULT 0, isSample DEFAULT 0, replacementCardId DEFAULT 0, serverVersion, isPendingEnqueue, isPendingApply
```
JSON `SerializableCard`: `id, isSeed?:bool, seedMod?, mod, serverMod, del, seedDel?, deckId, cardTypeId, created,
primaryField:str, secondaryField:str, fields:str, words:str, due, balancedDue, interval:double, factor:double,
lastReview, reviewCount, passCount, failCount, lapseCount, pos?, lessonId?:long|null, notes?:str, suspended?:bool,
isSample?:bool, replacementCardId?, serverVersion?, isPendingEnqueue?:bool, isPendingApply?:bool` (`?` = has default).

Encodings **[code]**:
- card field list = `[primaryField, secondaryField, *fields.split("\x1f")]` (`lw().uev = joinToString("\x1f")`).
- `words` = `"\x1f" + "\x1f".join(tokens) + "\x1f"`, token = `dictForm|secondary|partOfSpeech` (`WordToken.toString`; legacy separator `◴` U+25F4 accepted on read).

**card_type**: `id PK, mod, serverMod, del, lang TEXT, name TEXT, config TEXT, ...sync cols`.
`config` is JSON `CardTypeConfig {fields: [FieldInfo{name, type, align}], front, back, group}`;
`FieldType` enum `TEXT=0, SYNTAX=1, IMAGE=2, AUDIO=3, AUDIO_LONG=4` **[code @2783310]** (exact JSON of front/back templates not inspected).

**deck**: `id PK, mod, serverMod, del, lang, name, icon, lastRecalc, newBatchMax, newBatchSize, newGraduateCount,
factorMon..factorSun REAL, retention10/35/100/350/1000 REAL, intervalFactor10/35/100/350/1000 REAL,
learningMaterialId NULL, seedMod, seedDel, courseType TEXT DEFAULT "", ...sync cols`.

**review** (review log): `id PK (ms), mod, serverMod, del, day INT, interval REAL, factor REAL, cardId, deckId,
duration REAL, type INT, lapseIndex INT, ...sync cols`. `type`: `LEARN=0, FAIL=1, PASS=2` (`QX` enum).

**CardWordRelation**: `mod, serverMod, del, seedMod, seedDel, cardId, dictForm, secondary, partOfSpeech, language,
isTargetWord INT, occurrences INT, ...sync`; PK `(dictForm, secondary, partOfSpeech, language, isTargetWord, cardId)`.

**WordList** (JSON `words`): `dictForm, secondary, partOfSpeech, language, mod, serverMod, del, knownStatus TEXT
('UNKNOWN'|'LEARNING'|'KNOWN'|'IGNORED'), hasCard, tracked, created, isModern, ...sync`; PK `(dictForm, secondary, partOfSpeech, language)`.

**wordHistory**: `id, dictForm, secondary, partOfSpeech, language, mod, serverMod, del, day, knownStatus, prevKnownStatus NULL, origin TEXT, ...sync`; PK `(dictForm, secondary, partOfSpeech, language, day)`.

**reviewHistory**: `id PK, mod, serverMod, del, day, type INT, lang TEXT, ...sync` (daily study stats).

**config**: `id PK (always 0), mod, serverMod, config TEXT(JSON: reviewOrder, newUseGlobal, newMax, newBatchSize, newGraduateCount, zhAlternateReading, zhAlternateFields, zhCountingMode, lastLang, gradeOnFront), ...sync`.

**keyValue**: `key TEXT PK, entry TEXT, mod, serverMod, del, ...sync`.

**vacation**: `id, mod, serverMod, del, start, end, title, factor REAL, lang NULL, mode NULL, ...sync`.

**learningMaterial**, **lesson**, **library** (reader items): course/reader content, see schema dump in bundle @2543085/@2544550/@2545586.

Local-only (not synced): `local_data`, `localKeyValue`, `localOfflineMediaSync`, `LocalSync(rowId=0, lastPullSyncTimestamp, lastSyncedServerVersion)`, `langResource*`, `langVocabularyEntry`.

Useful queries seen **[code]**: due cards `WHERE balancedDue <= ? AND balancedDue > 0 AND del = 0 AND seedDel = 0 AND deckId = ?`;
new cards `WHERE due = 0 AND del = 0 AND seedDel = 0 AND deckId = ?`.

---

## 5. Scheduling (Migaku's own algorithm, **not FSRS**, not SM-2) **[code]**

Scheduler class `Xh` (logger `"Core - Scheduler"`, @~3896377) constants:
`cg4=1, dg4=.5, eg4=2, fg4=2.5, gg4=1.5, hg4=4.5, ig4=2, jg4=.25, kg4=.12, lg4=.35, mg4=1, ng4=1, og4=1, pg4=2`, and `Gh().ngq_1 = 20`.

Day numbers: `today = days between 2020-01-01 and localDate(now − 4 h)` in the user's timezone
(`dti`: `igq_1=new wu(2020,1,1)`, `jgq_1=4`) **[code; "minus 4 h" read from `i.np(4h)` – direction inferred]**.
`due`, `balancedDue`, `lastReview`, `review.day` all use this day number.

Entry: `Scheduler.reviewCardId(id, type, duration)` → refuses if study paused (`"pauseStudy/active"`).
Let `elapsed = today − card.lastReview`; `bucket(e) = deck.intervalFactor10 if e<=10, 35 if <=35, 100 if <=100, 350 if <=350 else intervalFactor1000` (`zye`).

| type | precondition | new card.interval `I` | new card.due | new card.factor | review.interval | review.factor |
|---|---|---|---|---|---|---|
| LEARN (0) | `due == 0` | `clamp(1·deck.intervalFactor10, 1, 2)` | `today + roundHalfEven(I)` | `deck.intervalFactor10` | 0 | 0 |
| FAIL (1) | `due != 0` | unchanged | `today` | unchanged | `elapsed` | card.factor |
| PASS (2) | `due != 0` | if `lapseCount>0`: `max(interval·0.25, 1)` else `max(elapsed·clamp(2.5·f,1.5,4.5), interval·1.5, 2)` with `f=bucket(elapsed)` | `today + ceil(I)` | `f = bucket(elapsed)` | `elapsed` | old card.factor |

Plus for every type (`OVt` / `kN` @~2727000) **[code]**:
- `reviewCount += (lastReview == today || lapseCount > 0) ? 0 : 1`
- `passCount += (PASS && !(lapseCount > 0 || lastReview == today)) ? 1 : 0`
- `failCount += (FAIL && lapseCount == 0) ? 1 : 0`
- `lapseCount = FAIL ? lapseCount + 1 : 0`
- card row: `UPDATE card SET mod=now, serverMod=-1, isPendingEnqueue=1, isPendingApply=0, due=D, balancedDue=D, interval=I, factor=F, lastReview=today, reviewCount, passCount, failCount, lapseCount WHERE id=?` (due bound twice → `balancedDue = due`).
- review row: `INSERT INTO review(id=nextId(ms), mod=now, serverMod=-1, del=0, day=today, interval, factor, cardId, deckId, duration, type, lapseIndex=<old lapseCount>, serverVersion=0, isPendingEnqueue=1, isPendingApply=0)`.
- PASS with `I >= 20` additionally marks the card's words `KNOWN` (WordList/wordHistory rows); LEARN calls a word-status update too (`Aye`, likely LEARNING) **[code for the call; exact rows not traced]**.
- Later deck recalculation ("Balancing reviews for deck", `factorMon..Sun`, `lastRecalc`) may move `balancedDue` **[code exists; algorithm not traced]**.
- `review.duration` unit **[unknown]** (Double from the UI; `SELECT sum(duration) FROM review WHERE id >= ?`).

To write a review back: push **both** the updated `cards[]` row and a new `reviews[]` row in one SyncObject.

---

## 6. Card creation **[code]**

`CardMgr.add(info: CardInfo{id?, deckId, typeId, fields: [String]})` → `Zrt` action → `abr(...)`:
```
INSERT INTO card(id, mod, serverMod, del, deckId, cardTypeId, created, primaryField, secondaryField, fields, words,
  due, balancedDue, interval, factor, lastReview, reviewCount, passCount, failCount, lapseCount, pos, lessonId,
  seedMod, seedDel, notes, serverVersion, isPendingEnqueue, isPendingApply)
VALUES (nextId, now, -1, 0, deckId, typeId, now, fields[0], fields[1], "\x1f".join(fields[2:]), words,
  0, 0, 0, 2.5, 0, 0, 0, 0, 0, 0, NULL, 0, 0, "", 0, 1, 0)
```
plus `CardWordRelation` rows (`isTargetWord` 1 for target words, 0 for the other parsed words; `language` = deck lang,
`occurrences` from the parser). `words`/relations come from Migaku's language parser **[code]**; whether the
server/other clients tolerate an empty `words` string and no relations is **[unknown]**.

Default card creator payload (`util.core-3e485932.js`, used by `getMemoryCreatePayload`) **[code]**:
```
fields = [targetWord, sentence, translation, definitions, exampleSentences, notes,
          images.r2Url  joined by "|", sentenceAudio.r2Url joined by "|", wordAudio.r2Url joined by "|",
          metadata ? JSON.stringify(metadata) : ""]
```
(for the standard Migaku card type; custom card types follow their `card_type.config.fields`).

### 6.1 Media upload **[code]** (@2997352, `OKt`)
- Host `FILE_SYNC_WORKER_HOST` default `https://file-sync-worker-api.migaku.com`.
- `PUT {host}/data/{category}/{fileName}` with Bearer, body = raw bytes (base64 data-URL decoded),
  headers `Content-Type: application/octet-stream`, `Content-Length`; optional query `displayName`, `fromLanguage`, `toLanguage`.
  `category` enum `UploadCategory`: `SRSMEDIA` (cards), `DICTIONARY`, `WORDLIST`, `FREQUENCY_LIST`, `BOOK`.
- Response JSON `{filePath: "..."}` → stored reference `"r2://" + filePath` (the `mediaFilePath`/`r2Url` placed in fields).
- Front-end converts before upload: audio/video → m4a, images → webp; file names like `<uuid>.webp` **[code in player-store]**.
- Read back: `GET {host}/data/<path after "r2://">` (Bearer). List: `GET {host}/data/<category>`.

---

## 7. DESTRUCTIVE endpoints – a client must never call these **[code]**

All on `https://srs-db-presigned-url-service-api.migaku.com` unless noted:

| endpoint | what the client does | effect |
|---|---|---|
| `GET /db-force-sync-upload-url` | then `PUT <url>` with `Content-Type: application/vnd.sqlite3`, body = gzip(local SQLite) | **Overwrites the whole server DB.** Log: `"This can override your database completely! Only use for testing and if you know what you are doing!"` |
| `GET /db-force-sync-delete-url` | then HTTP `DELETE <url>` | **Deletes the server DB.** Log: `"This will delete your database on the server completely! ..."` |
| `GET /db-backup-url` (query `target_device`, `migration_version`) | uploads compressed local DB | device backup upload (writes server storage) |
| `GET /db-server-backup` | — | triggers a server-side backup (side effect) |
| `overrideServerWithLocal` / `overrideLocalWithServer` | combinations of the above | conflict-resolution UI paths |
| `POST https://migaku-log-discord-forward.migaku.com/log` | "User Report" | posts a report to Migaku's Discord |

Safe/read-only: `/db-force-sync-download-url` (+ GET of the presigned URL), `/server-version`, `/pull-sync`,
`/learning-material*`. `/push/enqueue` writes rows (intended use) – only push rows you changed.

---

## 8. Open questions (need a live check with the test account)

1. Is the Firebase API key accepted from a non-browser origin (referrer restriction)?
2. Does `/pull-sync?serverVersion=0` return everything, or must we always start with the force download?
3. Exact pull JSON (are booleans `true/false` vs 0/1 in practice; are omitted defaults present) – decode leniently.
4. Push 409 trigger condition (probably `deviceVersion` < server version) and how long until pushed rows reappear in pull.
5. Do pushed rows need `serverMod: -1` exactly, and is `serverVersion` in pushed rows ignored?
6. Whether empty `words`/no CardWordRelation is acceptable for server-created cards; whether DataRepairMgr re-derives them.
7. `review.duration` unit; behaviour of `applicationEnvironment`/`coreVersion` gating.
8. python-lzo compatibility of the `LZ` short-header body (test by round-tripping against the JS decompressor if possible).
