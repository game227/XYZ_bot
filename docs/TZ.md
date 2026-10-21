# XYZ Telegram Bot — Yakuniy texnik topshiriq (TZ)

**Hujjat versiyasi:** 1.0 Final  
**Sana:** 21.10.2026  
**Loyiha nomi:** XYZ Telegram Education Bot  
**Platforma:** Telegram Bot  
**Asosiy til:** O‘zbek tili  
**Maqsadli yuklama:** 10 000+ ro‘yxatdan o‘tgan foydalanuvchi

---

## 1. Loyiha maqsadi

XYZ — o‘quvchilar, o‘qituvchilar va administratorlar uchun mo‘ljallangan mustaqil Telegram ta’lim tizimi.

Platforma orqali foydalanuvchilar:

- Fanlar, kurslar, modullar va darslarni ko‘rishi.
- Bepul darslarni o‘rganishi.
- Pullik obuna sotib olishi.
- Telegram ichida interaktiv testlar ishlashi.
- O‘z natijalari va o‘quv jarayonini kuzatishi.
- Dars materiallari va himoyalangan video darslardan foydalanishi.
- Bildirishnomalar va jonli darslar haqida ma’lumot olishi mumkin.

O‘qituvchilar dars materiallarini boshqaradi va ruxsat berilgan o‘quvchilar bilan ishlaydi. Administratorlar platforma, foydalanuvchilar, obunalar, testlar, darslar va xavfsizlikni boshqaradi.

### 1.1. Mustaqillik talabi

Telegram bot XYZ Django saytidan **to‘liq mustaqil** ishlashi shart.

- Django saytining API’lari ishlatilmaydi.
- Django saytining ma’lumotlar bazasiga ulanilmaydi.
- Saytdagi foydalanuvchilar bazasi bilan sinxronlash bo‘lmaydi.
- Bot o‘zining PostgreSQL ma’lumotlar bazasiga ega bo‘ladi.
- Botning fayllari va media saqlash tizimi alohida bo‘ladi.
- Botning autentifikatsiyasi, obunalari, testlari va rollari o‘z tizimi orqali boshqariladi.

## 2. Texnologiyalar

| Qism | Texnologiya |
|---|---|
| Dasturlash tili | Python 3.12+ |
| Telegram framework | aiogram 3.x |
| Ma’lumotlar bazasi | PostgreSQL |
| ORM | SQLAlchemy 2.x Async |
| Migratsiyalar | Alembic |
| PostgreSQL async drayveri | asyncpg |
| Konfiguratsiya | pydantic-settings |
| Fayllar saqlash | S3-compatible object storage |
| Video ko‘rsatish | HLS/DASH asosidagi himoyalangan player |
| Fon vazifalari | Zaruratga qarab Redis va worker |
| Joylashtirish | Linux VPS |
| Ishlash usuli | Dastlab Telegram long polling |
| Testlash | pytest va integratsion testlar |

Loyiha modulli, xavfsiz, kengaytiriladigan va ishlab chiqarish muhitiga mos yozilishi kerak.

---

## 3. Foydalanuvchi rollari

Tizimda uchta asosiy rol mavjud:

1. Administrator (`admin`)
2. O‘qituvchi (`teacher`)
3. O‘quvchi (`student`)

Rollar va ruxsatlar server tomonida tekshiriladi. Foydalanuvchi interfeysida tugmani yashirishning o‘zi xavfsizlik chorasi hisoblanmaydi.

### 3.1. Administrator

Administrator quyidagilarni boshqaradi:

- Foydalanuvchilar va ularning rollari.
- O‘qituvchilar hisoblari.
- O‘qituvchilarning faol sessiyalari.
- Fanlar, kurslar, modullar, mavzular va darslar.
- Dars materiallari va video fayllar.
- Testlar, savollar va javob variantlari.
- Obuna tariflari.
- Obuna so‘rovlari va to‘lovni qo‘lda tasdiqlash.
- Bepul foydalanish mumkin bo‘lgan darslar.
- Bildirishnomalar.
- Jonli darslar.
- Foydalanuvchilar faoliyati va tizim jurnallari.
- Bot sozlamalari va administratorlar vakolatlari.

Administratorlarning o‘z vakolat darajalari bo‘lishi mumkin. Yuqori huquqli amallar faqat tegishli ruxsatga ega administrator tomonidan bajariladi.

### 3.2. O‘qituvchi

O‘qituvchi quyidagi imkoniyatlarga ega bo‘ladi:

- Tizimga login va parol bilan kirish.
- O‘ziga biriktirilgan kurs va darslarni boshqarish.
- Ruxsat etilgan materiallarni yuklash va yangilash.
- Test va savollarni yaratish yoki tahrirlash — berilgan huquqlar doirasida.
- O‘ziga ruxsat berilgan o‘quvchilar bilan ishlash.
- Bot ichidagi ruxsat etilgan chatlar va yozishmalarni ko‘rish.
- O‘quv jarayoni va natijalarini ko‘rish — faqat ruxsat doirasida.

O‘qituvchi boshqa o‘qituvchining yopiq ma’lumotlarini, boshqa kurslarga tegishli maxfiy ma’lumotlarni yoki administrator funksiyalarini ko‘ra olmaydi.

### 3.3. O‘quvchi

O‘quvchi quyidagilardan foydalanadi:

- Fanlar va kurslar katalogi.
- Bepul darslar.
- Obuna sotib olish menyusi.
- Himoyalangan video player.
- Interaktiv testlar.
- O‘zining test natijalari va o‘quv jarayoni.
- Obuna holati va amal qilish muddati.
- Bildirishnomalar va yordam bo‘limi.

O‘quvchi boshqa foydalanuvchilarning shaxsiy ma’lumotlari yoki test natijalarini ko‘ra olmaydi.

---

## 4. Kurslar va darslar tuzilmasi

Ta’lim materiallari quyidagi ierarxiya bo‘yicha tashkil qilinadi:

**Fan → Kurs → Modul → Mavzu → Dars → Materiallar va testlar**

Har bir dars quyidagilarga ega bo‘lishi mumkin:

- Dars nomi.
- Qisqa tavsif.
- Matnli tushuntirish.
- Video.
- PDF yoki boshqa fayllar.
- Rasm va qo‘shimcha materiallar.
- Bir yoki bir nechta interaktiv test.
- Tartib raqami.
- Bepul yoki pullik maqomi.
- Faollik holati.
- Darsga kirish huquqi.

Administrator bepul darslarni belgilaydi. Pullik darslar faol obuna talab qiladi.

O‘qituvchilar faqat o‘zlariga biriktirilgan kurslar yoki administrator tomonidan ruxsat berilgan materiallarni boshqarishi mumkin.

---

## 5. Bepul darslar siyosati

Obunasi yo‘q foydalanuvchi butun bot bo‘yicha jami **2 ta bepul dars**dan foydalanishi mumkin.

### 5.1. Qoidalar

1. Ikki dars limiti har bir Telegram foydalanuvchisi uchun hisoblanadi.
2. Limit bitta kursga emas, butun botga tegishli.
3. Qaysi darslar bepul ekanini administrator belgilaydi.
4. Foydalanuvchi yangi bepul darsni birinchi marta muvaffaqiyatli ochganda limitdan bitta foydalanish hisoblanadi.
5. Bir darsni qayta ochish limitni yana kamaytirmaydi.
6. Foydalanuvchi bepul darsga biriktirilgan testlarni ishlashi mumkin.
7. Bepul darsga tegishli testlar uchun obuna talab qilinmaydi.
8. Pullik darslar va ularga biriktirilgan testlar uchun faol obuna kerak.
9. Bepul darslar limiti tugagach, bot obuna sotib olishni taklif qiladi.
10. Obuna muddati tugagach, avvalgi natijalar saqlanadi, lekin pullik materiallarga kirish yopiladi.

### 5.2. Texnik amalga oshirish

- Dars ochilganini foydalanuvchi va dars identifikatorlari bo‘yicha qayd etish.
- Bir darsni bir necha marta ochishni takroriy sarf sifatida hisoblamaslik.
- Bir vaqtning o‘zida yuborilgan parallel so‘rovlarda limit noto‘g‘ri sarflanmasligini ta’minlash.
- Bepul darsga kirish huquqini tekshirishda foydalanuvchining obuna holati va avval ochilgan darslarini hisobga olish.

Bir kishi bir nechta Telegram akkauntidan foydalansa, limitni mutlaq aniqlikda cheklash kafolatlanmaydi. Shubhali faollikni aniqlash uchun asosiy abuse monitoring va tezlik cheklovlari qo‘llanadi.

---

## 6. Obuna tariflari va to‘lovni tasdiqlash

Obuna tizimi dastlab administrator tomonidan qo‘lda tasdiqlash asosida ishlaydi. Avtomatik to‘lov provayderi bilan integratsiya ushbu versiya uchun majburiy emas.

### 6.1. Tariflar

Administrator tariflarni boshqaradi. Har bir tarif quyidagilarga ega bo‘ladi:

- Nomi.
- Narxi.
- Amal qilish muddati.
- Tavsifi.
- Faollik holati.
- Zarur bo‘lsa, foydalanish shartlari.

Tarif narxlari va davomiyligi administrator tomonidan belgilanadi.

### 6.2. O‘quvchi obuna olish jarayoni

1. O‘quvchi botning «Obuna olish» bo‘limiga kiradi.
2. Bot mavjud tariflarni ko‘rsatadi.
3. O‘quvchi tarifni tanlaydi.
4. Bot foydalanuvchining Telegram `user_id` raqamini ko‘rsatadi.
5. Bot to‘lov qilish va administratorga murojaat qilish bo‘yicha yo‘riqnoma beradi.
6. O‘quvchi administratorga o‘z Telegram ID raqami, tanlangan tarif va kerak bo‘lsa to‘lov dalilini yuboradi.
7. Administrator botning «Obuna so‘rovlari» bo‘limida so‘rovni ko‘radi.
8. Administrator to‘lovni tekshiradi.
9. Tasdiqlansa, bot obunani faollashtiradi va o‘quvchiga xabar yuboradi.
10. Rad etilsa, so‘rov holati va rad etish sababi, agar kiritilgan bo‘lsa, qayd etiladi.

Telegram ID foydalanuvchini aniqlashga yordam beradi, lekin o‘zi to‘lov qilinganini isbotlamaydi.

### 6.3. Obuna so‘rovlari

Har bir so‘rov quyidagilarni saqlaydi:

- Unikal so‘rov ID raqami.
- Foydalanuvchi ID raqami.
- Tanlangan tarif.
- So‘rov yaratilgan vaqt.
- To‘lov dalili yoki izohi — agar talab qilinsa.
- Holati: `pending`, `approved`, `rejected`, `cancelled`.
- Tekshirgan administrator.
- Qaror vaqti.
- Izoh.

Bir so‘rovni takroran tasdiqlab, bir foydalanuvchiga tasodifan bir nechta obuna yozib yuborishning oldi olinishi kerak.

### 6.4. Obuna holati

O‘quvchi quyidagilarni ko‘ra oladi:

- Faol yoki faol emasligi.
- Tarif nomi.
- Boshlanish sanasi.
- Tugash sanasi.
- Qolgan vaqt.
- Obuna olish yoki yangilash tugmasi.

Obuna muddati tugaganda pullik materiallarga kirish avtomatik ravishda yopiladi. Test natijalari va tarixiy ma’lumotlar o‘chirilmaydi.

### 6.5. Eslatmalar

Tizim obuna muddati tugashidan 3 kun va 1 kun oldin eslatma yuborishni qo‘llab-quvvatlashi kerak. Agar obuna 3 kunlik eslatma yuborish vaqtida allaqachon tugagan yoki uzaytirilgan bo‘lsa, eskirgan xabar yuborilmasligi kerak.

---

## 7. O‘qituvchi autentifikatsiyasi va 24 soatlik sessiya

Bu bo‘lim majburiy xavfsizlik talabidir.

### 7.1. O‘qituvchini ro‘yxatdan o‘tkazish

- O‘qituvchi hisobini faqat ruxsatli administrator yaratadi.
- Administrator o‘qituvchiga login va vaqtinchalik parol yoki xavfsiz aktivatsiya ma’lumotini beradi.
- O‘qituvchi bot orqali login va parolini kiritadi.
- Hisob administrator tomonidan bloklangan bo‘lsa, tizimga kirishga ruxsat berilmaydi.
- Birinchi kirishda vaqtinchalik parolni almashtirish talab qilinishi tavsiya etiladi.

### 7.2. Kirish jarayoni

1. O‘qituvchi «O‘qituvchi kabineti»ni tanlaydi.
2. Bot login so‘raydi.
3. Bot parol so‘raydi.
4. Tizim login va parolni tekshiradi.
5. Muvaffaqiyatli kirishdan so‘ng sessiya yaratiladi.
6. Sessiya aynan shu o‘qituvchining Telegram `user_id` raqamiga bog‘lanadi.
7. O‘qituvchi o‘z vakolatlari doirasidagi kabinetga kiradi.

### 7.3. Sessiyaning amal qilish muddati

- Sessiya muvaffaqiyatli autentifikatsiyadan keyin boshlanadi.
- Sessiyaning amal qilish muddati qat’iy 24 soat.
- 24 soatlik muddat har bir so‘rovda server tomonida tekshiriladi.
- Faollik sessiya muddatini avtomatik uzaytirmaydi.
- Muddat tugagach, o‘qituvchi qayta login qilishi kerak.
- Administrator sessiyani istalgan vaqtda bekor qila oladi.
- O‘qituvchi `/logout` buyrug‘i orqali sessiyani muddatidan oldin tugatishi mumkin.

### 7.4. Sessiya tugagandan keyingi cheklovlar

Sessiya muddati tugaganidan keyin o‘qituvchi quyidagi ma’lumotlarni ocholmaydi:

- Bot ichidagi o‘qituvchi kabineti.
- O‘qituvchiga ruxsat berilgan chatlar.
- Chatlar tarixi va yozishmalar.
- O‘quvchilar haqidagi shaxsiy ma’lumotlar.
- Darslar, topshiriqlar va boshqaruv funksiyalari.
- Test natijalari va boshqa o‘qituvchilik ma’lumotlari.
- Himoyalangan media yoki fayllar, agar ular o‘qituvchi sessiyasi bilan himoyalangan bo‘lsa.

Sessiya tugashi bilan mavjud sessiya tokeni yaroqsiz holatga o‘tkaziladi. Keyingi barcha so‘rovlarda sessiya holati va amal qilish muddati tekshiriladi.

**Muhim:** faqat menyu tugmalarini yashirish yetarli emas. Ma’lumotni qaytaradigan har bir handler, API endpoint yoki media-access endpoint sessiya va ruxsatni server tomonida tekshirishi shart.

### 7.5. Chatlar uchun alohida qoida

- Chatlar va yozishmalar botning o‘zida boshqariladigan tizimda saqlanishi va ko‘rsatilishi kerak.
- Chatni ochish yoki xabarlarni olishda faol o‘qituvchi sessiyasi tekshiriladi.
- Sessiya tugaganidan keyin yangi xabarlar, oldingi yozishmalar va chat ma’lumotlariga kirish bloklanadi.
- Sessiya tugashi chat tarixini ma’lumotlar bazasidan o‘chirmaydi.
- Qayta autentifikatsiyadan so‘ng o‘qituvchi o‘z vakolatlari doirasida chatlarni yana ko‘rishi mumkin.
- Administrator tomonidan sessiya bekor qilinsa, keyingi so‘rov darhol rad etiladi.

Telegram’ning oddiy shaxsiy chatida yuborilgan xabarlar foydalanuvchi qurilmasida qolishi mumkin. Bot o‘sha xabarlarni boshqa ilovalar yoki qurilmalardan mutlaq tarzda o‘chira olmaydi. Shu sababli himoyalangan o‘qituvchi yozishmalari bot ichidagi chat tizimida amalga oshiriladi.

### 7.6. Parol va sessiya xavfsizligi

- Parollar faqat xavfsiz, sekin xeshlash algoritmi orqali xeshlangan holda saqlanadi, masalan Argon2id yoki bcrypt.
- Ochiq parollar ma’lumotlar bazasida saqlanmaydi.
- Parollar loglarga yozilmaydi.
- Kirish urinishlariga rate limit qo‘llanadi.
- Ketma-ket noto‘g‘ri urinishlar vaqtinchalik cheklovga olib kelishi mumkin.
- Parolni bot chatida yuborish Telegram chat tarixida qolishi mumkin. Shu sababli bir martalik aktivatsiya kodi yoki xavfsiz parol kiritish sahifasi afzal.
- Sessiyalar uchun taxmin qilib bo‘lmaydigan identifikatorlar ishlatiladi.
- Sessiya tokenlari loglarda to‘liq ko‘rinishda saqlanmaydi.
- Administrator sessiyani bekor qilganida, keyingi so‘rovlarda darhol kuchini yo‘qotadi.

---

## 8. Interaktiv testlar

Testlar Telegram bot ichida ishlashi kerak. Tashqi saytga o‘tish asosiy test jarayoni uchun talab qilinmaydi.

### 8.1. Test turlari

- Bitta to‘g‘ri javobli savol.
- Bir nechta to‘g‘ri javobli savol.
- Zaruratga qarab, matnli javobli savol.
- Vaqt bilan cheklangan test.
- Tasodifiy tartibdagi savollar va javoblar.
- Bir necha urinishga ruxsat beriladigan test.

### 8.2. Test sozlamalari

Har bir test uchun quyidagilar sozlanishi mumkin:

- Test nomi va tavsifi.
- Bog‘langan dars.
- Savollar.
- Javob variantlari.
- To‘g‘ri javoblar.
- Maksimal ball.
- O‘tish chegarasi.
- Vaqt chegarasi.
- Urinishlar soni.
- Savollarni tasodifiy tartibda ko‘rsatish.
- Natijani ko‘rsatish qoidalari.
- Bepul yoki pullik kirish.

### 8.3. Testni ishlash

1. Foydalanuvchi darsni ochadi.
2. Tizim darsga kirish huquqini tekshiradi.
3. Foydalanuvchi darsga tegishli testni tanlaydi.
4. Test savollari ko‘rsatiladi.
5. Javoblar qabul qilinadi.
6. Test yakunlangach, tizim natijani hisoblaydi.
7. Natija ma’lumotlar bazasida saqlanadi.
8. Foydalanuvchiga ball, foiz va o‘tgan-o‘tmaganligi ko‘rsatiladi.

To‘g‘ri javoblar test yakunlanishidan oldin foydalanuvchiga yuborilmasligi kerak. Natija va javoblarni ko‘rsatish qoidalarini administrator belgilaydi.

### 8.4. Bepul va pullik testlar

- Obunasiz foydalanuvchi o‘zi uchun ruxsat berilgan ikkita bepul darsga biriktirilgan testlarni ishlashi mumkin.
- Pullik darsga tegishli testlar uchun faol obuna talab qilinadi.
- Obuna muddati tugagach, yangi pullik testni boshlash bloklanadi.
- Oldin bajarilgan testlar natijalari foydalanuvchining tarixida qoladi.
- Test davomida obuna yoki ruxsat holati o‘zgarsa, tizim natijani saqlash va davom ettirish qoidalarini izchil qo‘llashi kerak.

---

## 9. Video va media fayllarni himoyalash

Bu bo‘lim muhim funksional talab hisoblanadi.

### 9.1. Asosiy talab

**Himoyalangan video darslar Telegram’ga oddiy video yoki hujjat sifatida yuborilmasligi kerak.**

Buning sababi — Telegram ichida yuborilgan video yoki faylni foydalanuvchi yuklab olishi yoki boshqalarga uzatishi mumkin.

Bot videoni bevosita yuborish o‘rniga «Videoni ko‘rish» tugmasini ko‘rsatadi. Tugma himoyalangan video player sahifasini ochadi.

### 9.2. Media saqlash

- Video, PDF, audio va katta fayllar PostgreSQL ichida binary formatda saqlanmaydi.
- Media S3-compatible object storage yoki boshqa mos xususiy fayl saqlash xizmatida saqlanadi.
- PostgreSQL fayl metama’lumotlarini saqlaydi.
- Maxfiy fayllar ommaviy URL orqali ochilmaydi.
- Fayllar identifikatorlar va storage key orqali boshqariladi.
- Fayl yuklashda hajm, format va ruxsat tekshiriladi.
- Katta fayllar uchun uzilib qolgan yuklashni davom ettirish imkoniyati va checksum tekshiruvi qo‘llanadi.

### 9.3. Himoyalangan video player

- Foydalanuvchi avval bot orqali aniqlanadi.
- Video ochilishidan oldin obuna yoki o‘qituvchi ruxsati tekshiriladi.
- Video faqat ruxsatli foydalanuvchi uchun ochiladi.
- HLS yoki DASH orqali oqimli video uzatish qo‘llab-quvvatlanadi.
- Vaqtinchalik imzolangan URL yoki qisqa muddatli token ishlatiladi.
- Tokenlar muddati tugaganda qayta foydalanib bo‘lmaydi.
- Ruxsatlar server tomonida tekshiriladi.
- Imkon bo‘lsa, video ustida foydalanuvchining identifikatorini ko‘rsatuvchi dinamik watermark ishlatiladi.
- O‘qituvchi uchun media kirishi ham faol 24 soatlik sessiyaga bog‘lanadi.

### 9.4. Himoya chegaralari

Hech qanday oddiy web-player yoki bot ekran yozuvi va barcha nusxa olish usullarini mutlaq bloklay olmaydi.

Agar yuqori darajadagi DRM himoyasi talab qilinsa, DRM qo‘llaydigan maxsus video hosting xizmati alohida tanlanishi kerak. Bunday xizmatni tanlash xarajat, foydalanuvchilar soni, qurilmalar mosligi va DRM talablari bo‘yicha amalga oshiriladi.

Telegram Bot API orqali yangi fayl yuborish va `getFile` orqali fayl yuklab olishning standart hajm cheklovlari mavjud. Telegram Local Bot API Server ayrim yuklash imkoniyatlarini oshiradi, ammo video faylni to‘g‘ridan-to‘g‘ri Telegram orqali tarqatish himoyalangan player o‘rnini bosa olmaydi.

---

## 10. Bildirishnomalar

Tizim avtomatik va qo‘lda yuboriladigan bildirishnomalarni qo‘llab-quvvatlaydi.

### 10.1. Avtomatik bildirishnomalar

- Obuna faollashgani.
- Obuna muddati tugashiga 3 kun qolishi.
- Obuna muddati tugashiga 1 kun qolishi.
- Obuna tugagani.
- Yangi dars yoki material chiqarilgani.
- Jonli dars yaqinlashayotgani.
- Test yoki kurs bo‘yicha muhim eslatmalar.

### 10.2. Qo‘lda bildirishnomalar

Administrator:

- Barcha foydalanuvchilarga.
- Tanlangan rolga.
- Tanlangan kursga yozilgan foydalanuvchilarga.
- Muayyan foydalanuvchilarga xabar yubora oladi.

Xabar jo‘natish jarayonida tezlik cheklovlari, Telegram API xatolari, bloklangan botlar va yuborilmagan xabarlar hisobga olinadi.

Katta hajmdagi xabarlar fon vazifalari orqali navbat bilan yuboriladi. Yuborish natijalari qayd qilinadi.

---

## 11. Jonli darslar

Tizim jonli darslarni rejalashtirish va e’lon qilish imkoniyatiga ega bo‘ladi.

Har bir jonli dars uchun:

- Nomi.
- Tavsifi.
- Boshlanish vaqti.
- Bog‘langan kurs.
- O‘qituvchi.
- Ulanish havolasi.
- Ishtirok etish shartlari.
- Holati: rejalashtirilgan, boshlangan, yakunlangan yoki bekor qilingan.

O‘quvchi jonli dars haqida ma’lumot olishi va, agar ro‘yxatdan o‘tish funksiyasi yoqilgan bo‘lsa, qatnashish uchun ro‘yxatdan o‘tishi mumkin.

Jonli dars havolalari maxfiy bo‘lsa, ularni faqat tegishli ruxsatga ega foydalanuvchilarga ko‘rsatish kerak.

---

## 12. Ma’lumotlar bazasi tuzilmasi

Quyidagi jadvallar asosiy ma’lumotlar modelini tashkil qiladi. Yakuniy maydonlar, indekslar va cheklovlar implementatsiya vaqtida aniqlashtiriladi.

### 12.1. Foydalanuvchilar va ruxsatlar

- `users` — Telegram ID, rol, holat va asosiy ma’lumotlar.
- `user_settings` — foydalanuvchi sozlamalari.
- `teacher_accounts` yoki tegishli `users` kengaytmasi — o‘qituvchining login ma’lumotlari va holati.
- `teacher_sessions` — sessiya identifikatori, o‘qituvchi, Telegram ID, yaratilgan vaqti, tugash vaqti, bekor qilingan holati.
- `teacher_course_assignments` — o‘qituvchi va unga biriktirilgan kurslar.

Parollar uchun faqat hash saqlanadi. Sessiya ma’lumotlari o‘qituvchi va Telegram ID bilan bog‘lanadi.

### 12.2. Ta’lim materiallari

- `subjects`
- `courses`
- `course_teachers`
- `modules`
- `topics`
- `lessons`
- `media_files`
- `lesson_media`

### 12.3. Testlar

- `quizzes`
- `questions`
- `answer_options`
- `quiz_attempts`
- `attempt_answers`

### 12.4. Obunalar va to‘lovlar

- `subscription_plans`
- `subscriptions`
- `payment_requests`

`payment_requests` administrator tekshiruvi, so‘rov ID raqami, holat va qaror tarixini saqlaydi.

### 12.5. Foydalanish va aloqa

- `lesson_accesses` — foydalanuvchi qaysi bepul darsni birinchi marta ochganini qayd etish.
- `live_sessions`
- `live_session_registrations`
- `notifications`
- `notification_deliveries`
- `teacher_chat_threads`
- `teacher_chat_messages`
- `admin_audit_logs`
- `bot_settings`

Chat jadvallari faqat botning ichki chat funksiyasi amalga oshirilsa ishlatiladi. Chat ma’lumotlari uchun kirish nazorati o‘qituvchi sessiyasiga bog‘lanadi.

### 12.6. Ma’lumotlar yaxlitligi

- Telegram ID uchun mos `UNIQUE` cheklovi.
- Obuna va test yozuvlari uchun tashqi kalitlar.
- Muhim qidiruv maydonlari uchun indekslar.
- Takroriy to‘lov tasdiqlarini bloklaydigan cheklovlar.
- Bepul dars limitini parallel so‘rovlarda ham to‘g‘ri saqlash.
- Muhim holat o‘zgarishlari uchun tranzaksiyalar.
- Barcha vaqtlar bazada bir xil vaqt zonasida saqlanadi; foydalanuvchiga kerakli vaqt zonasi bo‘yicha ko‘rsatiladi.

---

## 13. Telegram bot menyusi

### 13.1. O‘quvchi menyusi

- 📚 Fanlar va kurslar
- 🎓 Bepul darslar
- 🧪 Testlar
- ▶️ Video darslar
- 💳 Obuna olish
- 📅 Jonli darslar
- 📈 Mening natijalarim
- 👤 Profilim
- 🔔 Bildirishnomalar
- ❓ Yordam

### 13.2. O‘qituvchi menyusi

- 🔐 O‘qituvchi kabinetiga kirish
- 📚 Mening kurslarim
- 🎬 Dars materiallari
- 🧪 Testlarni boshqarish — ruxsatga ko‘ra
- 💬 Chatlar
- 👨‍🎓 O‘quvchilar — ruxsatga ko‘ra
- 📊 Natijalar — ruxsatga ko‘ra
- 🔒 Chiqish (`/logout`)

Sessiya muddati tugaganida o‘qituvchiga yopiq ma’lumotlar ko‘rsatilmaydi. Bot qayta autentifikatsiya qilishni so‘raydi.

### 13.3. Administrator menyusi

- 👥 Foydalanuvchilar
- 👨‍🏫 O‘qituvchilar
- 🔐 O‘qituvchi sessiyalari
- 📚 Fanlar va kurslar
- 🎬 Darslar va media
- 🧪 Testlar
- 💳 Tariflar va obunalar
- 🧾 Obuna so‘rovlari
- 📅 Jonli darslar
- 📢 Bildirishnomalar
- 📊 Statistika
- 🧾 Audit jurnali
- ⚙️ Bot sozlamalari

---

## 14. Buyruqlar

Asosiy buyruqlar:

- `/start` — botni boshlash.
- `/menu` — asosiy menyu.
- `/profile` — profil.
- `/subscription` — obuna holati.
- `/logout` — o‘qituvchi sessiyasidan chiqish.
- `/help` — yordam.

Administrator buyruqlari rol va ruxsatlar bo‘yicha cheklanadi. Muhim boshqaruv amallari imkon qadar menyu orqali bajariladi va audit jurnaliga yoziladi.

---

## 15. Xavfsizlik talablari

1. Barcha maxfiy sozlamalar environment variables orqali boshqariladi.
2. `.env` fayli Git repository’ga kiritilmaydi.
3. PostgreSQL paroli, Telegram bot tokeni va storage kalitlari loglarda chiqmaydi.
4. Foydalanuvchi va o‘qituvchi ruxsatlari har bir himoyalangan so‘rovda tekshiriladi.
5. Administrator vakolatlari rollar bo‘yicha ajratiladi.
6. Login urinishlariga rate limit qo‘llanadi.
7. Parollar xesh ko‘rinishida saqlanadi.
8. SQLAlchemy parametrli so‘rovlari ishlatiladi.
9. Fayllar turi va hajmi tekshiriladi.
10. Himoyalangan media ommaviy storage URL’lari orqali ochilmaydi.
11. Sessiya tugaganda chat va o‘qituvchilik ma’lumotlariga kirish bloklanadi.
12. Obuna tugaganda pullik kontentga kirish yopiladi.
13. To‘lovni tasdiqlash faqat ruxsatli administrator tomonidan bajariladi.
14. Ma’lumotlarni o‘zgartiradigan muhim amallar audit jurnaliga yoziladi.
15. Xatolar foydalanuvchiga maxfiy texnik ma’lumotlarni oshkor qilmaydi.
16. Telegram xabarlaridagi foydalanuvchi kiritgan matnlar ishonchsiz ma’lumot sifatida qayta ishlanadi.
17. Chat va o‘quvchilar ma’lumotlari faqat vakolatli shaxslarga ko‘rsatiladi.
18. Sessiya tokenlari, parollar va maxfiy kalitlar oddiy loglarga yozilmaydi.

---

## 16. Audit jurnali

Quyidagi muhim hodisalar qayd qilinadi:

- Administrator tomonidan o‘qituvchi hisobi yaratilishi yoki bloklanishi.
- O‘qituvchining muvaffaqiyatli yoki muvaffaqiyatsiz autentifikatsiyasi.
- Sessiya yaratilishi, tugashi va bekor qilinishi.
- Tarif yaratilishi yoki o‘zgartirilishi.
- Obuna so‘rovi tasdiqlanishi yoki rad etilishi.
- Obunaning faollashtirilishi va tugashi.
- Darslar va testlar o‘zgartirilishi.
- Muhim ruxsat o‘zgarishlari.
- Administrator tomonidan qo‘lda yuborilgan bildirishnomalar.

Audit jurnali foydalanuvchi parollarini, sessiya tokenlarini yoki boshqa maxfiy ma’lumotlarni saqlamasligi kerak.

---

## 17. Statistika

Administrator quyidagi ko‘rsatkichlarni ko‘ra oladi:

- Jami foydalanuvchilar.
- Faol foydalanuvchilar.
- O‘qituvchilar soni.
- Faol obunalar.
- Muddati tugagan obunalar.
- Tasdiqlanishi kutilayotgan obuna so‘rovlari.
- Kurslar va darslar bo‘yicha faollik.
- Testlar soni va natijalari.
- Darslarni o‘zlashtirish ko‘rsatkichlari.
- Bildirishnomalar yuborilishi holati.

Statistika foydalanuvchi soni va ma’lumotlar hajmi oshganida ham samarali ishlashi kerak.

---

## 18. Ishlash tezligi va kengayish

Tizim 10 000 dan ortiq ro‘yxatdan o‘tgan foydalanuvchini qo‘llab-quvvatlashga mo‘ljallanadi.

Bu 10 000 foydalanuvchi bir vaqtning o‘zida faol bo‘lishini anglatmaydi. Haqiqiy bir vaqtdagi yuklama alohida o‘lchanadi va sinovdan o‘tkaziladi.

Talablar:

- Asinxron handlerlar.
- Asinxron PostgreSQL ulanishlari.
- Ma’lumotlar bazasi connection pool sozlamalari.
- Kerakli indekslar.
- Uzoq davom etadigan vazifalarni fon navbatiga chiqarish.
- Ommaviy bildirishnomalarni navbat bilan yuborish.
- Video va katta fayllarni bot jarayonidan alohida uzatish.
- Xatolar va ishlash ko‘rsatkichlarini kuzatish.
- Yuklama oshganda bot, worker va media xizmatlarini mustaqil kengaytirish imkoniyati.

Bir vaqtdagi foydalanuvchilar soni, VPS resurslari va video trafigi bo‘yicha aniq sig‘im yuklama testlari bilan tasdiqlanadi.

---

## 19. Zaxira nusxalar va tiklash

- PostgreSQL uchun avtomatik zaxira nusxalar.
- Media storage uchun alohida zaxira yoki versiyalash siyosati.
- Zaxira nusxalarga kirish cheklovlari.
- Zaxiralarni tiklash jarayonini sinovdan o‘tkazish.
- Muhim hodisalardan keyin tizimni tiklash tartibi.
- Xatoliklar haqida administratorga xabar berish.

Zaxira nusxalar faqat yaratilishi bilan cheklanmaydi; ularni tiklash mumkinligi ham tekshirilishi kerak.

---

## 20. Loyiha papkalari

Tavsiya etilgan boshlang‘ich tuzilma:

```text
xyz-telegram-bot/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── bot.py
│   ├── database/
│   │   ├── session.py
│   │   ├── base.py
│   │   └── models/
│   ├── handlers/
│   │   ├── common/
│   │   ├── student/
│   │   ├── teacher/
│   │   └── admin/
│   ├── keyboards/
│   ├── middlewares/
│   ├── services/
│   │   ├── auth.py
│   │   ├── teacher_sessions.py
│   │   ├── subscriptions.py
│   │   ├── lessons.py
│   │   ├── quizzes.py
│   │   ├── media.py
│   │   ├── notifications.py
│   │   └── audit.py
│   ├── repositories/
│   ├── schemas/
│   ├── utils/
│   └── workers/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── security/
├── scripts/
├── .env.example
├── .gitignore
├── alembic.ini
├── pyproject.toml
├── README.md
└── Dockerfile
```

Bu tuzilma tavsiya etilgan asosiy tuzilma bo‘lib, implementatsiya jarayonida modullarni mantiqan ajratish sharti bilan o‘zgartirilishi mumkin.

---

## 21. Amalga oshirish bosqichlari

### 1-bosqich: Loyiha asoslari

- Python muhitini tayyorlash.
- aiogram botini sozlash.
- PostgreSQL ulanishi.
- SQLAlchemy modellarini yaratish.
- Alembic migratsiyalari.
- Konfiguratsiya va maxfiy ma’lumotlarni boshqarish.

### 2-bosqich: Foydalanuvchilar va rollar

- `/start` va foydalanuvchini ro‘yxatdan o‘tkazish.
- Administrator, o‘qituvchi va o‘quvchi rollari.
- Ruxsatlar tekshiruvi.
- O‘qituvchi login tizimi.
- 24 soatlik sessiya va sessiyani bekor qilish.

### 3-bosqich: Ta’lim kontenti

- Fanlar va kurslar.
- Modullar, mavzular va darslar.
- Darsga material biriktirish.
- Bepul darslarni belgilash.
- Ikki bepul dars limitini amalga oshirish.

### 4-bosqich: Testlar

- Test va savollarni boshqarish.
- Telegram ichidagi test jarayoni.
- Natijalarni saqlash.
- Testga kirish huquqini tekshirish.
- Test tarixini ko‘rsatish.

### 5-bosqich: Obuna tizimi

- Tariflar.
- Obuna so‘rovlari.
- Administrator tasdig‘i.
- Obunani faollashtirish va tugatish.
- Obuna holatini ko‘rsatish.
- Eslatmalar.

### 6-bosqich: Media va chat xavfsizligi

- Xususiy media storage.
- Himoyalangan video player integratsiyasi.
- Vaqtinchalik kirish tokenlari.
- O‘qituvchi chatlari.
- Sessiya tugaganda chat va ma’lumotlarni bloklash.
- Ruxsatlarni tekshiruvchi testlar.

### 7-bosqich: Bildirishnomalar va jonli darslar

- Avtomatik xabarlar.
- Administratorning ommaviy xabarlari.
- Xabarlar navbati.
- Jonli darslarni boshqarish.

### 8-bosqich: Sinov va serverga joylashtirish

- Unit testlar.
- Integratsion testlar.
- Xavfsizlik sinovlari.
- Yuklama sinovlari.
- Zaxira nusxa va tiklash sinovi.
- VPS deployment.
- Monitoring va loglar.

---

## 22. Qabul qilish mezonlari

Loyiha quyidagi shartlar bajarilganda tayyor deb hisoblanadi:

1. Bot Django saytiga ulanmasdan ishlaydi.
2. Botning o‘z PostgreSQL bazasi mavjud.
3. Rollar va ruxsatlar to‘g‘ri tekshiriladi.
4. Administrator o‘qituvchi hisobini yarata oladi va bloklay oladi.
5. O‘qituvchi login va parol orqali tizimga kira oladi.
6. O‘qituvchi sessiyasi 24 soatdan keyin tugaydi.
7. Faollik sessiya muddatini uzaytirmaydi.
8. Sessiya tugaganda chatlar, yozishmalar va boshqa himoyalangan o‘qituvchi ma’lumotlari ochilmaydi.
9. Administrator sessiyani muddatidan oldin bekor qila oladi.
10. O‘qituvchi `/logout` orqali chiqib keta oladi.
11. Obunasiz foydalanuvchi jami ikkita bepul darsdan foydalana oladi.
12. Bir darsni qayta ochish bepul limitni yana kamaytirmaydi.
13. Bepul darslarga biriktirilgan testlar obunasiz ishlaydi.
14. Pullik dars va testlar uchun faol obuna tekshiriladi.
15. O‘quvchi tarif tanlay oladi va obuna so‘rovini yubora oladi.
16. Administrator to‘lovni tekshirib, obunani qo‘lda faollashtira oladi.
17. Takroriy tasdiqlash noto‘g‘ri yoki ikki karra obuna yaratmaydi.
18. Obuna tugaganda pullik materiallarga kirish yopiladi.
19. Test natijalari saqlanadi.
20. Himoyalangan video fayllari Telegram chatiga oddiy video fayl sifatida yuborilmaydi.
21. Media uchun ruxsat va vaqtinchalik kirish nazorati ishlaydi.
22. Avtomatik va qo‘lda bildirishnomalar ishlaydi.
23. Administratorning muhim amallari audit jurnalida qayd etiladi.
24. Zaxira nusxa olish va tiklash jarayoni sinovdan o‘tkaziladi.
25. Yuklama testlari kelishilgan foydalanuvchi va bir vaqtdagi so‘rovlar mezonlariga muvofiq o‘tadi.

---

## 23. Yakuniy loyiha tamoyillari

- XYZ Telegram bot mustaqil mahsulot sifatida ishlab chiqiladi.
- Foydalanuvchi, obuna, test va ta’lim ma’lumotlari o‘z bazasida saqlanadi.
- Rollar va kirish huquqlari server tomonida tekshiriladi.
- O‘qituvchining sessiyasi 24 soat bilan qat’iy cheklanadi.
- Sessiya tugashi o‘qituvchi chatlari va himoyalangan ma’lumotlarga kirishni bloklaydi.
- Obunasiz foydalanuvchi ikkita bepul dars va ularga biriktirilgan testlardan foydalanadi.
- Pullik obuna administrator tomonidan to‘lov tekshirilgach faollashtiriladi.
- Himoyalangan videolar Telegram fayli sifatida tarqatilmaydi; player orqali ko‘rsatiladi.
- Maxfiy ma’lumotlar, parollar va sessiya tokenlari himoyalanadi.
- Tizim 10 000+ ro‘yxatdan o‘tgan foydalanuvchiga mo‘ljallanadi va yuklama testlari bilan tekshiriladi.

**TZ yakunlandi.** Ushbu hujjat loyiha arxitekturasi, ma’lumotlar bazasi, funksiyalar, xavfsizlik va qabul qilish mezonlarini belgilaydi. Implementatsiya boshlanishidan oldin faqat texnik integratsiyalar uchun zarur aniq qiymatlar — masalan, VPS resurslari, media hosting provayderi va video himoyasi darajasi — tanlanishi kerak.
