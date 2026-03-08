# Raid Alarm — Kurulum Rehberi

## Gerekli Environment Variables

Railway dashboard → Variables sekmesinden şunları ekle:

| Değişken | Açıklama |
|---|---|
| `RUSTPLUS_SERVER_IP` | Rust sunucu IP adresi |
| `RUSTPLUS_SERVER_PORT` | Rust sunucu port (genellikle 28082) |
| `RUSTPLUS_STEAM_ID` | 64-bit Steam ID |
| `RUSTPLUS_PLAYER_TOKEN` | Rust+ player token |
| `RUSTPLUS_ENTITY_ID` | Sismik sensörün Entity ID'si |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_FROM_NUMBER` | Twilio'dan arayan numara (+1...) |
| `TWILIO_TO_NUMBER` | Aranacak telefon numarası (+90...) |

## Player Token & Entity ID Nasıl Alınır?

### Player Token
```
npx @liamcottle/rustplus.js fcm-register
```
(Google Chrome kurulu olmalı, Steam girişi ister)

### Entity ID
Rust oyununda sismik sensörü bir Smart Alarm'a bağla,
Rust+ uygulamasında alarm bildirimini al, entity ID'si bildirimde yazar.
