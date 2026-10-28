#pragma once
/*
 * The flash queue of readings that could not be sent (firmware 1.5.0).
 *
 * A row of segment files, /q/00000001.jl, /q/00000002.jl ..., each up to
 * SEG_BYTES, and a cursor: the segment and byte offset of the next unsent
 * line, saved in /q/cursor. Sending moves the cursor; a segment is deleted
 * once every line in it is sent. Nothing is ever copied, so draining needs
 * no free space, and a power cut mid-drain re-sends at most a few lines,
 * which the server recognises and stores once.
 *
 * Kept apart from main.cpp so it can be compiled and tested on a computer
 * (firmware/synthetic_wifi/test/queue_host_test.cpp) against a stand-in
 * filesystem. It needs from the sketch:
 */
int  sendBody(const String &body, bool quiet = false);   // HTTP status, or < 0 if the link failed
bool linkUp();                                           // is there a way home right now
void queueIdle();                                        // called between sends

/* Batches (1.6.0). With QUEUE_BATCH above 1 the queue drains up to that many
   readings per request, as one JSON array, to /readings/batch; the sketch
   then also provides
       int sendBatch(const String &jsonArray);
   qBatch can be lowered at run time (the Pi's config key b); 1 sends one at
   a time, exactly as 1.5.0 did. Left at 1, none of this is compiled. */
#ifndef QUEUE_BATCH
#define QUEUE_BATCH 1
#endif
#if QUEUE_BATCH > 1
int sendBatch(const String &body);
int qBatch = QUEUE_BATCH;
static const size_t BATCH_MAX_BYTES = 3600;      // under the 4 KB the modem carries cleanly in one write
static const int    MAX_BATCHES_PER_LOOP = 8;    // ~100 readings, inside the same 90 s budget
#endif

// ---------- queue ----------
static const char  *Q_DIR       = "/q";
static const char  *Q_CURSOR    = "/q/cursor";
static const char  *LEGACY_OLD  = "/buf.jsonl";      // where 1.4.0 kept its buffer
static const char  *LEGACY      = "/legacy.jsonl";   // kept, never sent
static const size_t SEG_BYTES   = 64UL * 1024UL;     // ~220 readings, ~18 h at 5 min
static const size_t MIN_FREE    = 96UL * 1024UL;     // room LittleFS needs to work
static const int    MAX_FLUSH_PER_LOOP = 20;         // do not stall the cycle
static const unsigned long FLUSH_BUDGET_MS = 90UL * 1000UL;

// The queue, in RAM: segments qHead..qTail, the next unsent line at byte
// qPos of segment qHead, and how many lines are waiting.
uint32_t qHead = 1, qTail = 1, qPos = 0;
int32_t  qCount = 0;
int32_t  legacyCount = 0;

// ============================ queue ============================
/*
 * Readings that could not be sent wait here, oldest first.
 *
 * A row of segment files, /q/00000001.jl, /q/00000002.jl ..., each up to
 * SEG_BYTES, and a cursor: the segment and byte offset of the next unsent
 * line, saved in /q/cursor. Sending moves the cursor. A segment is deleted
 * once every line in it is sent. Nothing is ever copied, so draining needs
 * no free space, and a power cut mid-drain re-sends at most a few lines,
 * which the server recognises and stores once.
 *
 * When flash runs short the oldest segment is dropped: about a day of the
 * oldest readings, where 1.4.0 tried to copy half the buffer, could not fit
 * the copy, and deleted the buffer anyway.
 */
static String segPath(uint32_t n) {
  char buf[24];
  snprintf(buf, sizeof(buf), "/q/%08lu.jl", (unsigned long)n);
  return String(buf);
}

static size_t freeBytes() {
  size_t total = LittleFS.totalBytes(), used = LittleFS.usedBytes();
  return used < total ? total - used : 0;
}

/* Newlines in a file from a byte offset: fast block reads, no Strings. */
static int32_t countLines(const char *path, uint32_t from) {
  File f = LittleFS.open(path, "r");
  if (!f) return 0;
  if (from) f.seek(from);
  uint8_t buf[256];
  int32_t n = 0;
  for (;;) {
    int got = f.read(buf, sizeof(buf));
    if (got <= 0) break;
    for (int i = 0; i < got; i++) if (buf[i] == '\n') n++;
  }
  f.close();
  return n;
}

static void cursorSave() {
  File f = LittleFS.open(Q_CURSOR, "w");
  if (!f) return;
  f.printf("%lu %lu\n", (unsigned long)qHead, (unsigned long)qPos);
  f.close();
}

static void cursorLoad() {
  qPos = 0;
  File f = LittleFS.open(Q_CURSOR, "r");
  if (!f) return;
  String s = f.readStringUntil('\n');
  f.close();
  int sp = s.indexOf(' ');
  if (sp < 0) return;
  uint32_t head = (uint32_t)s.substring(0, sp).toInt();
  uint32_t pos  = (uint32_t)s.substring(sp + 1).toInt();
  if (head != qHead) return;              // that segment is gone: start the next at its top
  File seg = LittleFS.open(segPath(qHead), "r");
  if (!seg) return;
  size_t size = seg.size();
  seg.close();
  if (pos <= size) qPos = pos;
}

void queueBegin() {
  if (!LittleFS.exists(Q_DIR)) LittleFS.mkdir(Q_DIR);

  if (LittleFS.exists(LEGACY_OLD) && !LittleFS.exists(LEGACY)) {
    LittleFS.rename(LEGACY_OLD, LEGACY);
    Serial.println("[queue] 1.4.0 readings moved to /legacy.jsonl - never sent, printed by D");
  }
  legacyCount = 0;
  if (LittleFS.exists(LEGACY))     legacyCount += countLines(LEGACY, 0);
  if (LittleFS.exists(LEGACY_OLD)) legacyCount += countLines(LEGACY_OLD, 0);

  // Find the oldest and newest segment.
  uint32_t lo = 0, hi = 0;
  File dir = LittleFS.open(Q_DIR);
  if (dir && dir.isDirectory()) {
    File f = dir.openNextFile();
    while (f) {
      String nm = f.name();                 // bare name on core 2.x, a path on 1.x
      int slash = nm.lastIndexOf('/');
      if (slash >= 0) nm = nm.substring(slash + 1);
      if (nm.endsWith(".jl")) {
        uint32_t n = (uint32_t)nm.toInt();
        if (n > 0 && (lo == 0 || n < lo)) lo = n;
        if (n > hi) hi = n;
      }
      f.close();
      f = dir.openNextFile();
    }
    dir.close();
  }
  if (lo) { qHead = lo; qTail = hi; } else { qHead = qTail = 1; }
  cursorLoad();

  qCount = 0;
  for (uint32_t n = qHead; n <= qTail; n++) {
    String p = segPath(n);
    if (LittleFS.exists(p)) qCount += countLines(p.c_str(), n == qHead ? qPos : 0);
  }
  Serial.printf("[queue] %ld waiting, %u KB of flash free\n", (long)qCount, (unsigned)(freeBytes() / 1024));
  if (legacyCount) {
    Serial.printf("[queue] %ld readings from 1.4.0 kept aside. Dump them with D, then C and Y to free the space.\n",
                  (long)legacyCount);
  }
}

static void dropOldestSegment() {
  String p = segPath(qHead);
  int32_t lost = LittleFS.exists(p) ? countLines(p.c_str(), qPos) : 0;
  LittleFS.remove(p);
  qHead++;
  qPos = 0;
  qCount -= lost;
  if (qCount < 0) qCount = 0;
  cursorSave();
  Serial.printf("[queue] flash short: dropped the %ld oldest readings\n", (long)lost);
}

/* A short write (flash full) or a cut leaves a line with no newline. */
static bool endsWithNewline(const String &p) {
  if (!LittleFS.exists(p)) return true;
  File f = LittleFS.open(p, "r");
  if (!f) return true;
  uint8_t c = '\n';
  size_t n = f.size();
  if (n) { f.seek(n - 1); f.read(&c, 1); }
  f.close();
  return c == '\n';
}

void queuePush(const String &body) {
  size_t need = body.length() + 1;
  while (freeBytes() < MIN_FREE + need && qHead < qTail) dropOldestSegment();
  if (freeBytes() < need + 8192) {
    Serial.println("[queue] flash full - this reading is lost");
    return;
  }

  if (!endsWithNewline(segPath(qTail))) qTail++;   // torn last line: never glue a reading onto it
  String p = segPath(qTail);
  File f = LittleFS.open(p, "a");
  if (f && f.size() >= SEG_BYTES) {          // this segment is full: start the next
    f.close();
    qTail++;
    p = segPath(qTail);
    f = LittleFS.open(p, "a");
  }
  if (!f) { Serial.println("[queue] could not open a segment"); return; }
  size_t wrote = f.print(body);
  wrote += f.print('\n');
  f.close();
  if (wrote != need) {
    Serial.printf("[queue] short write, %u of %u bytes - flash full?\n", (unsigned)wrote, (unsigned)need);
    return;
  }
  qCount++;
  Serial.printf("[queue] stored, %ld waiting\n", (long)qCount);
}

void queueClear() {
  for (uint32_t n = qHead; n <= qTail; n++) LittleFS.remove(segPath(n));
  LittleFS.remove(Q_CURSOR);
  LittleFS.remove(LEGACY);
  LittleFS.remove(LEGACY_OLD);
  qHead = qTail = 1;
  qPos = 0;
  qCount = 0;
  legacyCount = 0;
  Serial.println("[clear] queue and legacy readings deleted");
}

/* Mark a line as coming from the queue, so the server knows the time it
   arrived is not the time it was taken. */
static void markQueued(String &line) {
  if (line.endsWith("}") && line.indexOf("\"queued\"") < 0) {
    line.remove(line.length() - 1);
    line += ",\"queued\":1}";
  }
}

static bool permanentRefusal(int code) {
  // The server read it and will never take it: a bad line, not a bad link.
  return code == 400 || code == 413 || code == 422;
}

#if QUEUE_BATCH > 1
/* Up to qBatch lines of the head segment per request, as one JSON array.
   The cursor moves past a batch only once the server has answered 2xx, so a
   batch lost on the way is sent again whole, and the server stores each
   reading once. Returns false if the server refused a batch outright: the
   caller then sends those lines one at a time, and the bad one is skipped
   alone, never the eleven good ones with it. */
static bool queueFlushBatches(unsigned long start, int &sent) {
  int batches = 0;
  while (qCount > 0 && batches < MAX_BATCHES_PER_LOOP && millis() - start < FLUSH_BUDGET_MS) {
    String p = segPath(qHead);
    File f;
    if (LittleFS.exists(p)) f = LittleFS.open(p, "r");
    if (!f) {                                 // segment missing: move on
      if (qHead < qTail) { qHead++; qPos = 0; continue; }
      qCount = 0;
      break;
    }
    if (qPos > f.size()) qPos = f.size();
    f.seek(qPos);
    String body = "[";
    int n = 0;
    int32_t lines = 0;                        // newline-ended lines consumed (what qCount counts)
    uint32_t endPos = qPos;
    while (n < qBatch && f.available()) {
      uint32_t at = f.position();
      String line = f.readStringUntil('\n');
      uint32_t next = f.position();
      bool counted = next - at > line.length();
      line.trim();
      if (line.length() < 3 || line[0] != '{' || line[line.length() - 1] != '}') {   // blank or torn: dropped with this batch
        endPos = next;
        if (counted) lines++;
        continue;
      }
      markQueued(line);
      if (n > 0 && body.length() + line.length() + 2 > BATCH_MAX_BYTES) break;   // starts the next batch
      if (n > 0) body += ",";
      body += line;
      n++;
      endPos = next;
      if (counted) lines++;
    }
    bool atEnd = endPos >= f.size();
    f.close();
    if (n > 0) {
      body += "]";
      int code = sendBatch(body);
      if (code >= 200 && code < 300) {
        sent += n;
        batches++;
      } else if (permanentRefusal(code)) {
        Serial.printf("[queue] server refused a batch (HTTP %d): sending it one at a time\n", code);
        return false;
      } else {
        return true;                          // the link failed: everything stays for next loop
      }
    }
    qPos = endPos;
    qCount -= lines;
    if (qCount < 0) qCount = 0;
    cursorSave();
    if (atEnd) {                              // segment done: delete it
      LittleFS.remove(p);
      qPos = 0;
      if (qHead < qTail) qHead++;
      else qCount = 0;
    }
    queueIdle();                              // keep the camera answered
  }
  return true;
}
#endif

/* Send what is queued, oldest first. Stops at the first failure of the link
   and keeps the rest; a line the server refuses outright is skipped, so one
   torn line can never block everything behind it. */
void queueFlush() {
  if (qCount <= 0 || !linkUp()) return;
  unsigned long start = millis();
  int sent = 0, skipped = 0;
  bool stop = false;
#if QUEUE_BATCH > 1
  if (qBatch > 1 && queueFlushBatches(start, sent)) {
    if (qCount < 0) qCount = 0;
    cursorSave();
    if (sent) Serial.printf("[queue] sent %d in batches, %ld still waiting\n", sent, (long)qCount);
    return;
  }
#endif

  while (!stop && qCount > 0 && sent + skipped < MAX_FLUSH_PER_LOOP && millis() - start < FLUSH_BUDGET_MS) {
    String p = segPath(qHead);
    File f;
    if (LittleFS.exists(p)) f = LittleFS.open(p, "r");
    if (!f) {                                 // segment missing: move on
      if (qHead < qTail) { qHead++; qPos = 0; continue; }
      qCount = 0;
      break;
    }
    if (qPos > f.size()) qPos = f.size();
    f.seek(qPos);

    while (!stop && sent + skipped < MAX_FLUSH_PER_LOOP && millis() - start < FLUSH_BUDGET_MS && f.available()) {
      uint32_t at = f.position();
      String line = f.readStringUntil('\n');
      uint32_t next = f.position();
      bool counted = next - at > line.length();   // ended in '\n': qCount has it (a torn tail does not)
      line.trim();
      if (line.length() < 3 || line[0] != '{') {                           // blank or torn
        qPos = next;
        if (counted && qCount > 0) qCount--;
        continue;
      }
      markQueued(line);
      int code = sendBody(line, true);
      if (code >= 200 && code < 300) {
        qPos = next; if (counted) qCount--; sent++;
      } else if (permanentRefusal(code)) {
        qPos = next; if (counted) qCount--; skipped++;
        Serial.printf("[queue] server refused a line (HTTP %d), skipped\n", code);
      } else {
        stop = true;
      }
      if ((sent + skipped) % 5 == 0) cursorSave();
      queueIdle();                            // keep the camera answered
    }
    bool atEnd = !f.available();
    f.close();
    if (stop) break;
    if (atEnd) {                              // segment done: delete it
      LittleFS.remove(p);
      qPos = 0;
      if (qHead < qTail) qHead++;
      else qCount = 0;                        // that was the newest: nothing left
    }
  }
  if (qCount < 0) qCount = 0;
  cursorSave();
  if (sent || skipped) {
    Serial.printf("[queue] sent %d, skipped %d, %ld still waiting\n", sent, skipped, (long)qCount);
  }
}

static void dumpFile(const char *path, const char *prefix, uint32_t from, int &n) {
  File f = LittleFS.open(path, "r");
  if (!f) return;
  if (from) f.seek(from);
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() > 2) { Serial.print(prefix); Serial.println(line); n++; }
    delay(2);            // let the receiver keep up
  }
  f.close();
}

void dumpBuffer() {
  int n = 0;
  Serial.println("[dump] begin");
  if (LittleFS.exists(LEGACY))     dumpFile(LEGACY, "[legacy] ", 0, n);
  if (LittleFS.exists(LEGACY_OLD)) dumpFile(LEGACY_OLD, "[legacy] ", 0, n);
  for (uint32_t s = qHead; s <= qTail; s++) {
    String p = segPath(s);
    if (LittleFS.exists(p)) dumpFile(p.c_str(), "[payload] ", s == qHead ? qPos : 0, n);
  }
  Serial.printf("[dump] end, %d readings. Nothing was deleted.\n", n);
}
