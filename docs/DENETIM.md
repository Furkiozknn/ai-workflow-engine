# Denetim: ai-workflow-engine (30 Eylül 2026)

Yenilemeden önce `main` (`d564db7`, 0.1.0) üzerinde, bu makinede (Windows 11, Python 3.12, uv, Git Bash) ölçüldü; sunucu tarafı `ai-job-gateway` `main`'i `uvx` ile ayrı süreçte koştu. Ölçülmeyen bir şey yazılmadı. Ham çıktılar depo dışında: `kanit/ai-workflow-engine/{once,sonra}/komutlar.txt`.

## Kurulum ve ilk sonuç (boş klasör)

| Yol | Süre |
|---|---|
| `uvx --no-cache --from git+https://github.com/Furkiozknn/ai-workflow-engine awe --help` (boş önbellek) | 11,9 s |
| aynısı, ikinci çağrı | 13,4 s (git kaynağı her seferinde yeniden derleniyor) |
| `git clone` + `uv sync` | 4,6 s |
| `awe ...` (kurulu ortamda her çağrı) | 1,5-2,3 s |

"Tek komut, bir dakikada ilk sonuç" tutuyor. README yalnızca klon + `uv sync` yolunu anlatıyordu; `uvx` yolu yoktu, eklendi.

## README komutları

| Komut | Sonuç |
|---|---|
| `awe validate examples/generate-and-upscale.yaml` | çalıştı, çıktı README'deki gibi |
| `awe run ... --var prompt=...` | ilk örnek `media` eklentisi olan bir gateway ister; eklentisiz gateway'de `generate-image` (barındırılan Pollinations, istem makineden çıkar) çalışıp `media-upscale`'de düştü: iş gönderilmiş, sonra hata |
| `awe run` sunucusuz | `error: step 'a' failed: All connection attempts failed` (4,4 s bekledikten sonra) |
| `uv run pytest` | 84 geçti, 1 atlandı (gateway yok); gateway ile 92 (project-meta.json, 28 Eylül ölçümü; bu denetimde yeniden ölçülmedi) |
| `python arac/vendor-dogrula.py` | README'deki komut; ağ gerektirir, bu denetimde koşulmadı |

## Bulunan sorunlar

| Girdi | Önce | Sorun |
|---|---|---|
| Sunucu yokken `awe run` | "All connection attempts failed", hangi adres olduğu ve ne yapılacağı yok; hata "step 'a' failed" diye adım hatası gibi görünüyor | **Hata** (yanıltıcı) |
| Gateway'de olmayan capability | önceki katmanlar çoktan çalışmış, ancak o adıma gelince `submission rejected (404): {"detail":...}` ham JSON | **Hata**: iş ve (hosted capability'de) kota harcandıktan sonra düşüyor; gateway'in `/v1/capabilities` ucu var ama kullanılmıyordu |
| `awe validate .` (klasör) | `cannot read .: Permission denied` | yanlış neden (Windows klasör için PermissionError veriyor) |
| `depends_on` yazım hatası | `depends on unknown step 'dratf'` | şablon başvurusundaki "did you mean" önerisi burada yoktu |
| `--help` | `usage` + alt komut adı; açıklama, örnek, çıkış kodu yok; `file`, `--gateway-url`, `--var` yardım metni yok; `--version` yok | eksik |

Sorunsuz bulunanlar: `validate` çıktısı, yok/bozuk dosya hataları (tek satır), `--var` ön kontrolü, `--timeout` ve pacing bayrakları, çıkış kodları (0/1/2).

## README bulguları

- İlk ekran GIF + reel + uzun anlatıyla açılıyordu; `docs/reel/*` ve `assets/demo.gif` üreticisiz olduğu için çıkarıldı (git geçmişinde duruyor). Yerine gerçek oturum çıktısı kondu.
- "Ne zaman kullanılır / kullanılmaz" yoktu; eklendi.
- Kütüphane API'si: `PipelineError`, `PipelineRunError`, `StepResult`, `Step`, `Pipeline`, `parse_pipeline_str` docstring'siz ya da tek satırdı; paket `help()` çıktısında kullanım örneği yoktu. Eklendi. Anahtar gerektiren yol yok; örnek gateway'in yerel `mock-generate`/`echo`'sunu kullanıyor ve çıktıyı "mock" diye işaretliyor.
- Test sayısı 92 (`project-meta.json`) -> yenileme sonrası 106 (gateway kuruluyken), gatewaysiz 97 geçti + 1 atlandı.
- Günlük "Ekosistem denetimi" konusu (#19): bu depoya ait bulgular yalnızca meta-source ayrışması (92 ↔ 80, summary ↔ description); kapatılmadı.
