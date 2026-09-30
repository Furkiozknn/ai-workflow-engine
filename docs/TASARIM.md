# Tasarım: ai-workflow-engine arayüz yenilemesi

## Hedef
İlk ekranda: tek cümle tanım, tek komutlu kurulum (`uvx --from git+... awe --help`), gerçek çıktılı kısa oturum, "ne zaman kullanılır / kullanılmaz". Yanlış kullanımda iz ya da yanıltıcı adım hatası yerine, ilk işten önce gelen, ne yapılacağı belli tek satır.

## Önce / sonra
| | Önce | Sonra |
|---|---|---|
| README girişi | banner + reel GIF + GIF + iki uzun paragraf | banner, tek cümle, `uvx` komutu, gerçek oturum, kullanım sınırları |
| Sunucusuz `awe run` | `step 'a' failed: All connection attempts failed` | `error: cannot reach the gateway at URL (...); is it running? e.g. ai-job-gateway serve`, hiçbir iş gitmeden |
| Gateway'de olmayan capability | önceki katmanlar koştuktan sonra ham 404 JSON | `error: the gateway at URL does not offer capability 'x'; it offers: ...` hiçbir iş gitmeden |
| `depends_on` yazım hatası | `unknown step 'dratf'` | `... 'dratf' (did you mean 'draft'?)` |
| `awe validate <klasör>` | `Permission denied` | `it is a directory, not a pipeline file` |
| `--help` | usage | açıklama, örnekler, çıkış kodları, her argüman için metin, `--version` |
| Kütüphane | çoğu genel sınıf docstring'siz | docstring + `help(ai_workflow_engine)` içinde çalışır örnek |
| Yerel deneme | anahtar/eklenti gerektiren örnekler | `examples/mock-chain.yaml`: gateway'in yerleşik `mock-generate`/`echo`'su, anahtarsız |
| Test | 92 | 106 |

## Akış
`awe validate FILE` (hiçbir şey çalıştırmaz) -> `awe run FILE --gateway-url URL --var K=V`: dosyayı yükle, `--var` eksiklerini denetle, **gateway'e `GET /v1/capabilities` sor** (ulaşılamıyor ya da capability yok: çıkış 1, iş gönderilmez), sonra katman katman çalıştır, sonucu JSON bas. `/v1/capabilities` olmayan bir sunucu hata sayılmaz, denetim atlanır. Kütüphanede bu denetim varsayılan kapalı (`check_capabilities=False`): mevcut çağıranların istek sayısı değişmez. Çıkış kodları ve JSON çıktı sözleşmesi değişmedi.

## Görsel kimlik
Terminal videosu (`terminal-uret.py`, `komutlar.txt`'ten üretilir) FRK-OS renkleriyle (`sosyal/uret/tema.mjs`: siyah #0e0d0b, krem #f1ece2, sarı #ffc21a; kaynak dosyadan sadece renk değerleri alındı) ve yerel JetBrains Mono (OFL, kanıt klasöründe) ile. Kontrast, siyah zemin üzerinde hesaplandı: krem 16,5:1, sarı 12,0:1, soluk gri #8f897c 5,6:1, hata rengi #ff8a6b 8,4:1 (hepsi ≥ 4,5:1). README'de görsel yok, düz metin bloğu (kopyalanabilir, kaynağı gerçek çıktı).
