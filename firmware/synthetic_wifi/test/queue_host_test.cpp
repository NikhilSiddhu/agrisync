/*
 * Host test for the 1.5.0 flash queue (src/queue.h), on the in-memory
 * filesystem in test/arduino_shim.h. No board, no serial port.
 *
 *   cd firmware/synthetic_wifi
 *   g++ -std=c++17 -Wall -I src -I test test/queue_host_test.cpp -o qtest && ./qtest
 *
 * Exits 1 if any check fails. QTEST_CANARY=1 adds one check that must fail,
 * to prove a broken assertion is not swallowed.
 *
 * What it covers (MASTER_STATE.md 4.2):
 *   push/flush order, segment rollover, a fully sent segment deleted
 *   the cursor: saved every 5 lines, survives a reboot (nothing re-sent,
 *     nothing skipped), a power cut mid-drain re-sends at most 4 lines
 *   20 lines and 90 s per flush; ,"queued":1 appended
 *   HTTP 400/413/422 skipped, any other failure stops and retries next loop
 *   MIN_FREE: the oldest segment dropped, never the whole queue
 *   short writes (failWritesAfter) and torn last lines; qCount stays true
 *   /buf.jsonl -> /legacy.jsonl, never sent
 *   D: [legacy] and [payload] lines, "[dump] end, N readings", deletes nothing
 */
#include <algorithm>
#include <cstdio>
#include <deque>
#include <functional>
#include <string>
#include <vector>

#include "arduino_shim.h"
#include "queue.h"

// ------------------------------------------------------------ checks
static int g_pass = 0, g_fail = 0;
static const char *g_case = "";

static void failed(const char *file, int line, const std::string &what) {
  g_fail++;
  printf("  FAIL [%s] %s:%d: %s\n", g_case, file, line, what.c_str());
}
#define CHECK(c) do { if (c) g_pass++; else failed(__FILE__, __LINE__, #c); } while (0)
#define CHECK_EQ(a, b) do {                                                       \
    long long _a = (long long)(a), _b = (long long)(b);                           \
    if (_a == _b) g_pass++;                                                       \
    else failed(__FILE__, __LINE__, std::string(#a " == " #b "  (") +            \
                std::to_string(_a) + " vs " + std::to_string(_b) + ")");          \
  } while (0)
#define CHECK_STR(a, b) do {                                                      \
    std::string _a = (a), _b = (b);                                               \
    if (_a == _b) g_pass++;                                                       \
    else failed(__FILE__, __LINE__, std::string(#a " == " #b "\n      got:  ") +  \
                _a.substr(0, 300) + "\n      want: " + _b.substr(0, 300));        \
  } while (0)

static void beginCase(const char *name) {
  g_case = name;
  printf("== %s\n", name);
}

// ------------------------------------------------------------ the sketch's side
struct PowerCut {};

struct FakeServer {
  bool link = true;
  std::vector<std::string> calls;          // every body offered
  std::vector<std::string> got;            // bodies accepted (2xx)
  std::deque<int> script;                  // forced codes for the next calls
  unsigned long costMs = 0;                // time each send takes
  std::function<void(int, const std::string &)> hook;   // before each send, 1-based
  int idle = 0;
  bool allQuiet = true;
};
static FakeServer srv;

/* What the real server does with a body: one flat-or-nested JSON object
   carrying a device_id. A torn line or two lines glued together is a 400. */
static bool wellFormed(const std::string &b) {
  if (b.size() < 2 || b.front() != '{' || b.back() != '}') return false;
  if (b.find("\"device_id\"") == std::string::npos) return false;
  int depth = 0;
  bool inStr = false;
  for (size_t i = 0; i < b.size(); i++) {
    char c = b[i];
    if (inStr) { if (c == '\\') i++; else if (c == '"') inStr = false; continue; }
    if (c == '"') inStr = true;
    else if (c == '{') depth++;
    else if (c == '}') { depth--; if (depth == 0 && i != b.size() - 1) return false; }
  }
  return depth == 0 && !inStr;
}

int sendBody(const String &body, bool quiet) {
  if (!quiet) srv.allQuiet = false;
  srv.calls.push_back(body.s);
  if (srv.hook) srv.hook((int)srv.calls.size(), body.s);
  g_millis += srv.costMs;
  int code;
  if (!srv.script.empty()) { code = srv.script.front(); srv.script.pop_front(); }
  else code = wellFormed(body.s) ? 200 : 400;
  if (code >= 200 && code < 300) srv.got.push_back(body.s);
  return code;
}
bool linkUp() { return srv.link; }
void queueIdle() { srv.idle++; }

// ------------------------------------------------------------ helpers
static const char *DEV = "esp32-smoke-01";

/* A 1.5.0 reading, about 250 bytes like the real ones, with a seq to check order. */
static std::string mk(int seq) {
  char b[512];
  snprintf(b, sizeof(b),
           "{\"device_id\":\"%s\",\"firmware\":\"1.5.0\",\"boot\":7,\"up_s\":%d,\"seq\":%d,"
           "\"sampled_at\":%ld,\"sensors\":{\"bme280\":true,\"mlx90614\":false,\"ads1115\":true},"
           "\"air_temp_c\":24.31,\"humidity_pct\":99.9,\"pressure_hpa\":993.2,"
           "\"rain_analog\":812,\"rain_tips\":3,\"rssi\":-79,\"buffered_count\":%d}",
           DEV, seq * 300, seq, 1793000000L + seq * 300L, seq);
  return b;
}
static std::string mkLegacy(int n) {
  char b[256];
  snprintf(b, sizeof(b),
           "{\"device_id\":\"%s\",\"firmware\":\"1.4.0\",\"legacy_n\":%d,\"air_temp_c\":21.0,"
           "\"humidity_pct\":100.0,\"rain_tips\":53600000,\"buffered_count\":%d}", DEV, n, n);
  return b;
}
static std::string queued(const std::string &line) {   // what the server should receive
  return line.substr(0, line.size() - 1) + ",\"queued\":1}";
}
static long seqOf(const std::string &body) {
  size_t p = body.find("\"seq\":");
  return p == std::string::npos ? -1 : atol(body.c_str() + p + 6);
}
static std::string seg(uint32_t n) { return segPath(n).s; }
static bool has(const std::string &p) { return g_fs.files.count(p) > 0; }
static std::string &fdata(const std::string &p) { return g_fs.files[p]; }
static int nl(const std::string &s) { return (int)std::count(s.begin(), s.end(), '\n'); }
static int segFiles() {
  int n = 0;
  for (auto &kv : g_fs.files) if (kv.first.rfind("/q/", 0) == 0 && kv.first.size() > 3 && kv.first.find(".jl") != std::string::npos) n++;
  return n;
}
static std::string cursorFile() { return has(Q_CURSOR) ? fdata(Q_CURSOR) : std::string("<none>"); }
static std::string said(size_t from) { return Serial.out.substr(std::min(from, Serial.out.size())); }
static bool saidHas(size_t from, const std::string &x) { return said(from).find(x) != std::string::npos; }

/* What a reboot would count: newlines from the cursor to the end of the queue,
   worked out from the files without touching the queue's RAM state. */
static int32_t diskCount() {
  int32_t n = 0;
  for (uint32_t s = qHead; s <= qTail; s++) {
    if (!has(seg(s))) continue;
    const std::string &d = fdata(seg(s));
    size_t from = s == qHead ? std::min<size_t>(qPos, d.size()) : 0;
    n += (int32_t)std::count(d.begin() + from, d.end(), '\n');
  }
  return n;
}

/* A power cycle: RAM is garbage, only flash survives. */
static void reboot() {
  qHead = 777; qTail = 3; qPos = 123456; qCount = -5; legacyCount = 99;
  srv.hook = nullptr;
  srv.script.clear();
  queueBegin();
}
/* A board straight out of the box (or with whatever files the case puts in first). */
static void wipe() {
  g_fs = FsState();
  Serial.out.clear();
  srv = FakeServer();
  g_millis = 0;
}
static void fresh() { wipe(); reboot(); }

static std::vector<std::string> pushN(int from, int n) {
  std::vector<std::string> v;
  for (int i = from; i < from + n; i++) { v.push_back(mk(i)); queuePush(String(v.back())); }
  return v;
}
static std::vector<size_t> offsets(const std::vector<std::string> &lines) {
  std::vector<size_t> off(1, 0);
  for (auto &l : lines) off.push_back(off.back() + l.size() + 1);
  return off;
}
static std::string cur(uint32_t head, size_t pos) {
  return std::to_string(head) + " " + std::to_string(pos) + "\n";
}

// ============================================================ cases

static void t_order() {
  beginCase("push/flush order, rollover at 64 KB, sent segment deleted");
  fresh();
  const int N = 600;
  std::vector<std::string> lines = pushN(0, N);
  CHECK_EQ(qCount, N);
  CHECK(qTail >= 3);
  CHECK_EQ(qHead, 1);
  bool sizesOk = true;
  for (uint32_t s = 1; s < qTail; s++) {
    size_t sz = fdata(seg(s)).size();
    if (sz < SEG_BYTES || sz >= SEG_BYTES + 400) sizesOk = false;
  }
  CHECK(sizesOk);                                   // each full segment just past 64 KB, then the next
  std::string all, want;
  for (uint32_t s = 1; s <= qTail; s++) all += fdata(seg(s));
  for (auto &l : lines) want += l + "\n";
  CHECK(all == want);                               // stored oldest first, byte for byte

  reboot();
  CHECK_EQ(qCount, N);
  CHECK_EQ(qHead, 1);

  int loops = 0;
  bool capOk = true, deletedOk = true;
  while (qCount > 0 && loops < 100) {
    size_t before = srv.got.size();
    queueFlush();
    loops++;
    if (srv.got.size() - before > (size_t)MAX_FLUSH_PER_LOOP) capOk = false;
    for (uint32_t s = 1; s < qHead; s++) if (has(seg(s))) deletedOk = false;
  }
  CHECK(capOk);
  CHECK(deletedOk);                                 // every segment behind the head is gone
  CHECK_EQ(loops, N / 20);
  CHECK_EQ(srv.got.size(), N);
  CHECK_EQ(srv.calls.size(), N);
  bool exact = srv.got.size() == (size_t)N;
  for (int i = 0; exact && i < N; i++) exact = srv.got[i] == queued(lines[i]);
  CHECK(exact);                                     // same order, ,"queued":1 appended, nothing else changed
  if (!srv.got.empty()) CHECK_STR(srv.got[0].substr(srv.got[0].size() - 12), ",\"queued\":1}");
  CHECK_EQ(segFiles(), 0);
  CHECK_EQ(qCount, 0);
  CHECK_EQ(srv.idle, N);                            // camera serviced after every send
  CHECK(srv.allQuiet);                              // queued sends are quiet

  queueFlush();                                     // empty: nothing happens
  CHECK_EQ(srv.calls.size(), N);
  reboot();
  CHECK_EQ(qCount, 0);
  std::vector<std::string> more = pushN(N, 3);      // a drained queue fills again
  queueFlush();
  CHECK_EQ(srv.got.size(), N + 3);
  CHECK(srv.got.back() == queued(more.back()));
}

static void t_link_down() {
  beginCase("no link: nothing sent, nothing moved");
  fresh();
  pushN(0, 10);
  srv.link = false;
  std::map<std::string, std::string> snap = g_fs.files;
  queueFlush();
  CHECK_EQ(srv.calls.size(), 0);
  CHECK_EQ(qCount, 10);
  CHECK(g_fs.files == snap);
}

static void t_caps() {
  beginCase("at most 20 lines and 90 s per flush");
  fresh();
  pushN(0, 30);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 20);
  CHECK_EQ(qCount, 10);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 30);
  CHECK_EQ(qCount, 0);

  // refusals count against the 20 too: a run of bad lines cannot stall the cycle
  fresh();
  pushN(0, 30);
  for (int i = 0; i < 30; i++) srv.script.push_back(400);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 20);
  CHECK_EQ(qCount, 10);

  // 10 s a send: sends start at 0..80 s, the one due at 90 s waits
  fresh();
  pushN(0, 30);
  srv.costMs = 10000;
  g_millis = 5000;
  queueFlush();
  CHECK_EQ(srv.calls.size(), 9);
  CHECK_EQ(qCount, 21);
  CHECK_EQ(g_millis - 5000, 90000);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 18);
  CHECK_EQ(qCount, 12);
  bool order = true;
  for (size_t i = 0; i < srv.got.size(); i++) order = order && seqOf(srv.got[i]) == (long)i;
  CHECK(order);
}

static void t_cursor_every_5() {
  beginCase("cursor saved every 5 lines");
  fresh();
  std::vector<std::string> lines = pushN(0, 12);
  std::vector<size_t> off = offsets(lines);
  std::vector<std::string> seen(13);
  srv.hook = [&](int k, const std::string &) { if (k <= 12) seen[k] = cursorFile(); };
  queueFlush();
  for (int k = 1; k <= 5; k++)  CHECK_STR(seen[k], "<none>");
  for (int k = 6; k <= 10; k++) CHECK_STR(seen[k], cur(1, off[5]));
  for (int k = 11; k <= 12; k++) CHECK_STR(seen[k], cur(1, off[10]));
  CHECK_EQ(srv.got.size(), 12);
  CHECK(!has(seg(1)));                              // newest segment fully sent: deleted
  CHECK_STR(cursorFile(), cur(1, 0));
  CHECK_EQ(qCount, 0);
}

static void t_reboot() {
  beginCase("cursor survives a reboot: nothing re-sent, nothing skipped");
  // (a) the link fails part way, the box reboots, the drain resumes exactly there
  fresh();
  std::vector<std::string> lines = pushN(0, 12);
  std::vector<size_t> off = offsets(lines);
  srv.script = {200, 200, 200, 200, 200, 200, 200, 503};
  queueFlush();
  CHECK_EQ(srv.got.size(), 7);
  CHECK_EQ(qCount, 5);
  CHECK_STR(cursorFile(), cur(1, off[7]));
  reboot();
  CHECK_EQ(qHead, 1);
  CHECK_EQ(qPos, off[7]);
  CHECK_EQ(qCount, 5);
  queueFlush();
  CHECK_EQ(srv.got.size(), 12);
  bool exact = srv.got.size() == 12;
  for (int i = 0; exact && i < 12; i++) exact = srv.got[i] == queued(lines[i]);
  CHECK(exact);

  // (b) stopped by the 20-line cap across two segments, then a reboot
  fresh();
  lines = pushN(0, 300);                            // two segments
  CHECK_EQ(qTail, 2);
  int inSeg1 = nl(fdata(seg(1)));
  int flushes = inSeg1 / 20 + 1;                    // a little way into segment 2
  for (int i = 0; i < flushes; i++) queueFlush();
  CHECK_EQ(srv.got.size(), flushes * 20);
  CHECK(flushes * 20 > inSeg1);
  CHECK_EQ(qHead, 2);
  CHECK(!has(seg(1)));
  uint32_t pos = qPos;
  reboot();
  CHECK_EQ(qHead, 2);
  CHECK_EQ(qPos, pos);
  CHECK_EQ(qCount, 300 - flushes * 20);
  while (qCount > 0 && srv.calls.size() < 400) queueFlush();
  CHECK_EQ(srv.got.size(), 300);
  exact = srv.got.size() == 300;
  for (int i = 0; exact && i < 300; i++) exact = srv.got[i] == queued(lines[i]);
  CHECK(exact);

  // (c) power cut during the 8th send: the cursor says 5, so 6 and 7 go twice (the
  // server stores them once) and nothing is lost
  fresh();
  lines = pushN(0, 12);
  srv.hook = [](int k, const std::string &) { if (k == 8) throw PowerCut(); };
  bool cut = false;
  try { queueFlush(); } catch (const PowerCut &) { cut = true; }
  CHECK(cut);
  CHECK_EQ(srv.got.size(), 7);
  reboot();
  queueFlush();
  std::vector<long> seqs;
  for (auto &b : srv.got) seqs.push_back(seqOf(b));
  CHECK_EQ(seqs.size(), 14);
  std::vector<long> want = {0, 1, 2, 3, 4, 5, 6, 5, 6, 7, 8, 9, 10, 11};
  CHECK(seqs == want);
  CHECK_EQ(qCount, 0);

  // (d) stale or bad cursor files never skip a line
  wipe();
  LittleFS.mkdir(Q_DIR);
  fdata(seg(2)) = mk(0) + "\n" + mk(1) + "\n";
  fdata(seg(3)) = mk(2) + "\n";
  fdata(Q_CURSOR) = "1 200\n";                     // segment 1 is gone: start 2 at the top
  reboot();
  CHECK_EQ(qHead, 2); CHECK_EQ(qTail, 3); CHECK_EQ(qPos, 0); CHECK_EQ(qCount, 3);
  fdata(Q_CURSOR) = "2 999999\n";                  // past the end of the file
  reboot();
  CHECK_EQ(qPos, 0); CHECK_EQ(qCount, 3);
  fdata(Q_CURSOR) = "garbage";
  reboot();
  CHECK_EQ(qPos, 0); CHECK_EQ(qCount, 3);
  fdata(Q_CURSOR) = cur(2, mk(0).size() + 1);       // a good one is honoured
  reboot();
  CHECK_EQ(qPos, mk(0).size() + 1); CHECK_EQ(qCount, 2);
  queueFlush();
  CHECK_EQ(srv.got.size(), 2);
  if (srv.got.size() == 2) { CHECK_EQ(seqOf(srv.got[0]), 1); CHECK_EQ(seqOf(srv.got[1]), 2); }
}

static void t_http() {
  beginCase("HTTP 400/413/422 skipped; any other failure stops and retries next loop");
  fresh();
  std::vector<std::string> lines = pushN(0, 6);
  std::vector<size_t> off = offsets(lines);
  srv.script = {200, 400, 413, 422, 500};
  size_t mark = Serial.out.size();
  queueFlush();
  CHECK_EQ(srv.calls.size(), 5);
  CHECK_EQ(srv.got.size(), 1);
  CHECK_EQ(qCount, 2);
  CHECK_EQ(qPos, off[4]);
  CHECK_STR(cursorFile(), cur(1, off[4]));
  CHECK(saidHas(mark, "[queue] server refused a line (HTTP 400), skipped"));
  CHECK(saidHas(mark, "(HTTP 413), skipped"));
  CHECK(saidHas(mark, "(HTTP 422), skipped"));
  CHECK(saidHas(mark, "[queue] sent 1, skipped 3, 2 still waiting"));
  queueFlush();                                     // next loop: the 500 line again, then the last
  CHECK_EQ(srv.calls.size(), 7);
  if (srv.calls.size() == 7) {
    CHECK(srv.calls[5] == queued(lines[4]));
    CHECK(srv.calls[6] == queued(lines[5]));
  }
  CHECK_EQ(qCount, 0);
  int reoffered = 0;                                // refused lines are never offered again
  for (size_t i = 5; i < srv.calls.size(); i++)
    for (int k = 1; k <= 3; k++) if (srv.calls[i] == queued(lines[k])) reoffered++;
  CHECK_EQ(reoffered, 0);

  const int transient[] = {-11, -1, 0, 301, 302, 401, 403, 404, 408, 429, 500, 502, 503, 504};
  for (int code : transient) {
    fresh();
    lines = pushN(0, 3);
    srv.script = {200, code};
    queueFlush();
    bool ok = srv.calls.size() == 2 && srv.got.size() == 1 && qCount == 2 && qPos == lines[0].size() + 1;
    queueFlush();
    ok = ok && srv.calls.size() == 4 && srv.calls[2] == queued(lines[1]) && qCount == 0;
    if (!ok) failed(__FILE__, __LINE__, "HTTP " + std::to_string(code) + " should stop the flush and retry the same line");
    else g_pass++;
  }
}

static void t_min_free() {
  beginCase("MIN_FREE 96 KB: the oldest segment dropped, never the whole queue");
  fresh();
  std::vector<std::string> lines;
  bool dropped = false, lostEarly = false;
  int i = 0;
  while (i < 20000) {
    uint32_t h = qHead;
    int32_t c = qCount;
    size_t freeBefore = freeBytes();
    int headLines = has(seg(qHead)) ? nl(fdata(seg(qHead))) : 0;
    size_t mark = Serial.out.size();
    lines.push_back(mk(i));
    queuePush(String(lines.back()));
    i++;
    if (saidHas(mark, "dropped the")) {
      dropped = true;
      printf("  first drop at reading %d: %ld held in %lu segments, %u KB free before it\n",
             i, (long)c, (unsigned long)(qTail - 1), (unsigned)(freeBefore / 1024));
      CHECK_EQ(h, 1);
      CHECK_EQ(qHead, 2);
      CHECK(!has(seg(1)));
      CHECK(freeBefore < MIN_FREE + lines.back().size() + 1);   // not dropped early
      CHECK(freeBefore + 2 * 4096 >= MIN_FREE);                  // nor late
      CHECK_EQ(qCount, c + 1 - headLines);
      CHECK(saidHas(mark, "[queue] flash short: dropped the " + std::to_string(headLines) + " oldest readings"));
      CHECK(saidHas(mark, "[queue] stored"));
      CHECK(qTail - qHead >= 10);
      CHECK_STR(cursorFile(), cur(2, 0));
      break;
    }
    if (!saidHas(mark, "[queue] stored")) { lostEarly = true; break; }
  }
  CHECK(dropped);
  CHECK(!lostEarly);

  // keep going: each drop takes exactly the head, the rest stays, nothing is refused
  bool eachOk = true;
  int drops = 1;
  for (int k = 0; k < 1500; k++) {
    uint32_t h = qHead;
    size_t mark = Serial.out.size();
    lines.push_back(mk(i));
    queuePush(String(lines.back()));
    i++;
    if (!saidHas(mark, "[queue] stored")) eachOk = false;
    if (saidHas(mark, "dropped the")) {
      drops++;
      if (qHead != h + 1 || has(seg(h)) || !has(seg(qHead))) eachOk = false;
    }
    if (freeBytes() + 4096 < MIN_FREE) eachOk = false;
  }
  CHECK(eachOk);
  CHECK(drops >= 5);
  CHECK_EQ(diskCount(), qCount);
  int32_t held = qCount;
  long firstKept = seqOf(fdata(seg(qHead)).substr(0, fdata(seg(qHead)).find('\n')));
  for (int k = 0; k < 400 && qCount > 0; k++) queueFlush();
  CHECK_EQ(srv.got.size(), held);
  bool contiguous = !srv.got.empty() && seqOf(srv.got.front()) == firstKept && seqOf(srv.got.back()) == i - 1;
  for (size_t k = 1; contiguous && k < srv.got.size(); k++) contiguous = seqOf(srv.got[k]) == seqOf(srv.got[k - 1]) + 1;
  CHECK(contiguous);                                // only whole oldest segments went, newest all kept

  // one segment and flash short: it is kept and still takes readings
  fresh();
  pushN(0, 10);
  std::string seg1 = fdata(seg(1));
  size_t avail = freeBytes();
  fdata("/filler") = std::string(avail - 40 * 1024, 'x');
  CHECK_EQ(freeBytes(), 40 * 1024);
  size_t mark = Serial.out.size();
  queuePush(String(mk(10)));
  CHECK(!saidHas(mark, "dropped"));
  CHECK(saidHas(mark, "[queue] stored, 11 waiting"));
  CHECK(has(seg(1)));
  CHECK_EQ(qCount, 11);
  // and with less than a reading's worth plus 8 KB, the new reading is the one lost
  fdata("/filler") += std::string(freeBytes() - 4096, 'x');
  seg1 = fdata(seg(1));
  mark = Serial.out.size();
  queuePush(String(mk(11)));
  CHECK(saidHas(mark, "[queue] flash full - this reading is lost"));
  CHECK(fdata(seg(1)) == seg1);
  CHECK_EQ(qCount, 11);

  // four segments, 4 KB free: only as many oldest segments go as it takes
  fresh();
  pushN(0, 800);                                    // 3 full segments and a 4th
  CHECK_EQ(qTail, 4);
  std::string keep3 = fdata(seg(3)), keep4 = fdata(seg(4));
  g_fs.files["/filler"] = std::string(freeBytes() - 4096, 'x');
  mark = Serial.out.size();
  queuePush(String(mk(800)));
  CHECK_EQ(qHead, 3);                               // two segments (~136 KB) were enough
  CHECK(!has(seg(1)) && !has(seg(2)));
  CHECK(fdata(seg(3)) == keep3);
  CHECK(fdata(seg(4)).compare(0, keep4.size(), keep4) == 0);
  CHECK(saidHas(mark, "[queue] stored"));
  CHECK_EQ(qCount, diskCount());

  // two segments, 4 KB free: dropping the older is not enough, the newest stays
  fresh();
  pushN(0, 300);
  CHECK_EQ(qTail, 2);
  int in1 = nl(fdata(seg(1)));
  std::string keep2 = fdata(seg(2));
  g_fs.files["/filler"] = std::string(freeBytes() - 4096, 'x');
  mark = Serial.out.size();
  queuePush(String(mk(300)));
  CHECK(freeBytes() < MIN_FREE);                    // still short, yet ...
  CHECK_EQ(qHead, 2);                               // ... the newest segment is never dropped
  CHECK(fdata(seg(2)).compare(0, keep2.size(), keep2) == 0);
  CHECK(saidHas(mark, "[queue] stored"));
  CHECK_EQ(qCount, 300 - in1 + 1);
  CHECK_EQ(qCount, diskCount());
}

static void t_short_write() {
  beginCase("short write (flash full mid-line): later readings are not damaged");
  fresh();
  std::vector<std::string> l = {mk(0), mk(1), mk(2), mk(3), mk(4), mk(5)};
  for (int k = 0; k < 3; k++) queuePush(String(l[k]));
  g_fs.failWritesAfter = 100;
  size_t mark = Serial.out.size();
  queuePush(String(l[3]));
  CHECK(saidHas(mark, "[queue] short write, 100 of"));
  CHECK_EQ(qCount, 3);
  g_fs.failWritesAfter = -1;
  queuePush(String(l[4]));
  queuePush(String(l[5]));
  CHECK_EQ(qCount, 5);
  CHECK_EQ(diskCount(), qCount);                    // a reboot would count the same
  queueFlush();
  std::vector<std::string> want = {queued(l[0]), queued(l[1]), queued(l[2]), queued(l[4]), queued(l[5])};
  CHECK(srv.got == want);                           // 4 and 5 intact; the torn 3 never accepted
  CHECK_EQ(qCount, 0);
  CHECK_EQ(segFiles(), 0);

  // the same, rebooting between the short write and the next reading
  fresh();
  for (int k = 0; k < 3; k++) queuePush(String(l[k]));
  g_fs.failWritesAfter = 100;
  queuePush(String(l[3]));
  g_fs.failWritesAfter = -1;
  reboot();
  CHECK_EQ(qCount, 3);
  queuePush(String(l[4]));
  CHECK_EQ(diskCount(), qCount);
  queueFlush();
  want = {queued(l[0]), queued(l[1]), queued(l[2]), queued(l[4])};
  CHECK(srv.got == want);
  CHECK_EQ(qCount, 0);

  // only the newline failed: the reading itself is whole
  fresh();
  queuePush(String(l[0]));
  g_fs.failWritesAfter = (int)l[1].size();
  queuePush(String(l[1]));
  g_fs.failWritesAfter = -1;
  queuePush(String(l[2]));
  CHECK_EQ(qCount, 2);
  CHECK_EQ(diskCount(), qCount);
  queueFlush();
  bool got0 = false, got2 = false;
  int got1 = 0;
  for (auto &b : srv.got) { got0 |= b == queued(l[0]); got1 += b == queued(l[1]); got2 |= b == queued(l[2]); }
  CHECK(got0);
  CHECK(got2);
  CHECK(got1 <= 1);
  printf("  newline-only failure: reading 1 %s\n", got1 ? "recovered" : "not recovered");
  CHECK_EQ(qCount, 0);
  CHECK_EQ(segFiles(), 0);
}

static void t_torn() {
  beginCase("torn last line, garbage lines; qCount decremented and kept true");
  std::string L0 = mk(0), L1 = mk(1), L2 = mk(2), L3 = mk(3);

  // (a) the newest segment ends in half a line
  wipe();
  LittleFS.mkdir(Q_DIR);
  fdata(seg(1)) = L0 + "\n" + L1 + "\n" + L2.substr(0, 100);
  reboot();
  CHECK_EQ(qCount, 2);                              // only whole lines are counted
  queueFlush();
  std::vector<std::string> want = {queued(L0), queued(L1)};
  CHECK(srv.got == want);
  CHECK_EQ(qCount, 0);
  CHECK_EQ(segFiles(), 0);

  // (b) blank and garbage lines: skipped without a send, each one off qCount
  wipe();
  LittleFS.mkdir(Q_DIR);
  std::string d = "\n" + std::string("garbage-not-json\n") + "  \r\n" + "x\n";
  std::vector<std::string> good;
  for (int k = 0; k < 25; k++) { good.push_back(mk(k)); d += good.back() + "\n"; }
  fdata(seg(1)) = d;
  reboot();
  CHECK_EQ(qCount, 29);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 20);                   // garbage never offered, not in the 20
  CHECK_EQ(srv.got.size(), 20);
  CHECK_EQ(qCount, 5);
  CHECK_EQ(diskCount(), qCount);
  queueFlush();
  CHECK_EQ(srv.got.size(), 25);
  CHECK_EQ(qCount, 0);

  // (c) a torn line that ends an older segment: everything after it still goes
  wipe();
  LittleFS.mkdir(Q_DIR);
  fdata(seg(1)) = L0 + "\n" + L1 + "\n" + L2.substr(0, 100);
  fdata(seg(2)) = L2 + "\n" + L3 + "\n";
  reboot();
  CHECK_EQ(qCount, 4);
  srv.script = {200, 200, 400, 503};                // stop on the first line of segment 2
  queueFlush();
  CHECK_EQ(qHead, 2);
  CHECK_EQ(qCount, 2);                              // L2 and L3 are still waiting
  CHECK_EQ(diskCount(), qCount);
  queueFlush();
  want = {queued(L0), queued(L1), queued(L2), queued(L3)};
  CHECK(srv.got == want);
  CHECK_EQ(qCount, 0);
  CHECK_EQ(segFiles(), 0);

  wipe();                                           // same, in one flush
  LittleFS.mkdir(Q_DIR);
  fdata(seg(1)) = L0 + "\n" + L1 + "\n" + L2.substr(0, 100);
  fdata(seg(2)) = L2 + "\n" + L3 + "\n";
  reboot();
  queueFlush();
  CHECK(srv.got == want);
  CHECK_EQ(qCount, 0);
  CHECK_EQ(segFiles(), 0);

  // (d) power cut left a torn tail; after the reboot a new reading is pushed
  wipe();
  LittleFS.mkdir(Q_DIR);
  fdata(seg(1)) = L0 + "\n" + L1.substr(0, 120);
  reboot();
  CHECK_EQ(qCount, 1);
  queuePush(String(L2));
  CHECK_EQ(qCount, 2);
  CHECK_EQ(diskCount(), qCount);
  queueFlush();
  want = {queued(L0), queued(L2)};
  CHECK(srv.got == want);                           // L2 not glued to the torn L1
  CHECK_EQ(qCount, 0);
  CHECK_EQ(segFiles(), 0);
}

static void t_mark() {
  beginCase(",\"queued\":1 appended once; CR LF lines");
  wipe();
  LittleFS.mkdir(Q_DIR);
  std::string already = "{\"device_id\":\"esp32-smoke-01\",\"seq\":0,\"queued\":1}";
  std::string crlf = "{\"device_id\":\"esp32-smoke-01\",\"seq\":1}";
  fdata(seg(1)) = already + "\n" + crlf + "\r\n";
  reboot();
  queueFlush();
  CHECK_EQ(srv.got.size(), 2);
  if (srv.got.size() == 2) {
    CHECK_STR(srv.got[0], already);
    CHECK_STR(srv.got[1], queued(crlf));
  }
}

static void t_legacy() {
  beginCase("/buf.jsonl -> /legacy.jsonl, counted, never sent, kept");
  wipe();
  std::string leg;
  for (int k = 0; k < 3; k++) leg += mkLegacy(k) + "\n";
  fdata(LEGACY_OLD) = leg;
  reboot();
  CHECK(!has(LEGACY_OLD));
  CHECK(has(LEGACY));
  CHECK(fdata(LEGACY) == leg);
  CHECK_EQ(legacyCount, 3);
  CHECK_EQ(qCount, 0);
  CHECK(saidHas(0, "[queue] 1.4.0 readings moved to /legacy.jsonl"));
  CHECK(saidHas(0, "[queue] 3 readings from 1.4.0 kept aside"));
  queueFlush();
  CHECK_EQ(srv.calls.size(), 0);                    // empty queue: legacy alone is never sent
  pushN(0, 2);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 2);
  bool anyLegacy = false;
  for (auto &b : srv.calls) anyLegacy |= b.find("1.4.0") != std::string::npos;
  CHECK(!anyLegacy);
  CHECK(fdata(LEGACY) == leg);
  size_t mark = Serial.out.size();
  reboot();                                         // no second rename, same count
  CHECK(!saidHas(mark, "moved to"));
  CHECK_EQ(legacyCount, 3);
  CHECK(fdata(LEGACY) == leg);

  // a /legacy.jsonl already there is never overwritten; both are counted
  wipe();
  fdata(LEGACY) = leg;
  fdata(LEGACY_OLD) = mkLegacy(9) + "\n";
  reboot();
  CHECK(fdata(LEGACY) == leg);
  CHECK(has(LEGACY_OLD));
  CHECK_EQ(legacyCount, 4);
  pushN(0, 1);
  queueFlush();
  CHECK_EQ(srv.calls.size(), 1);
}

static void t_dump() {
  beginCase("D: [legacy] and [payload] lines, [dump] end, N readings; deletes nothing");
  wipe();
  std::vector<std::string> leg = {mkLegacy(0), mkLegacy(1)};
  fdata(LEGACY_OLD) = leg[0] + "\n" + leg[1] + "\n";
  reboot();
  std::vector<std::string> q = pushN(0, 5);
  srv.script = {200, 200, 500};
  queueFlush();                                     // 0 and 1 sent; 2..4 waiting
  CHECK_EQ(qCount, 3);
  std::map<std::string, std::string> snap = g_fs.files;
  std::set<std::string> dirs = g_fs.dirs;
  uint32_t h = qHead, t = qTail, p = qPos;
  int32_t c = qCount, lc = legacyCount;
  size_t mark = Serial.out.size();
  dumpBuffer();
  std::string want = "[dump] begin\n"
                     "[legacy] " + leg[0] + "\n"
                     "[legacy] " + leg[1] + "\n"
                     "[payload] " + q[2] + "\n"
                     "[payload] " + q[3] + "\n"
                     "[payload] " + q[4] + "\n"
                     "[dump] end, 5 readings. Nothing was deleted.\n";
  CHECK_STR(said(mark), want);
  CHECK(g_fs.files == snap);
  CHECK(g_fs.dirs == dirs);
  CHECK_EQ(qHead, h); CHECK_EQ(qTail, t); CHECK_EQ(qPos, p);
  CHECK_EQ(qCount, c); CHECK_EQ(legacyCount, lc);
  srv.script.clear();
  queueFlush();                                     // the dump changed nothing: the rest still goes
  CHECK_EQ(srv.got.size(), 5);
  CHECK_EQ(qCount, 0);

  // across segments, from the cursor on, in order
  fresh();
  q = pushN(0, 300);
  queueFlush();                                     // 20 sent
  snap = g_fs.files;
  mark = Serial.out.size();
  dumpBuffer();
  std::string out = said(mark);
  std::vector<std::string> payload;
  size_t a = 0;
  while (a < out.size()) {
    size_t e = out.find('\n', a);
    std::string line = out.substr(a, e - a);
    if (line.rfind("[payload] ", 0) == 0) payload.push_back(line.substr(10));
    a = e + 1;
  }
  bool exact = payload.size() == 280;
  for (int k = 0; exact && k < 280; k++) exact = payload[k] == q[20 + k];
  CHECK(exact);
  CHECK(out.find("[legacy]") == std::string::npos);
  CHECK(out.find("[dump] end, 280 readings") != std::string::npos);
  CHECK(g_fs.files == snap);

  // empty
  fresh();
  mark = Serial.out.size();
  dumpBuffer();
  CHECK_STR(said(mark), "[dump] begin\n[dump] end, 0 readings. Nothing was deleted.\n");
}

static void t_canary() {
  beginCase("canary (QTEST_CANARY set): this check must fail");
  CHECK_EQ(1 + 1, 3);
}

int main() {
  t_order();
  t_link_down();
  t_caps();
  t_cursor_every_5();
  t_reboot();
  t_http();
  t_min_free();
  t_short_write();
  t_torn();
  t_mark();
  t_legacy();
  t_dump();
  if (getenv("QTEST_CANARY")) t_canary();
  printf("\n%d checks passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
