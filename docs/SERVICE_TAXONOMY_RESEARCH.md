# Service taxonomy: what goes in `ServiceCategory` → `Service`

**Status:** catalogue loaded and verified on a real database.
**Deliverables:** `services/catalogue/service_taxonomy.py` (the data), `home/management/commands/seed_service_taxonomy.py` (the loader), `home/test_service_taxonomy.py` (7 tests), this document, and `service_taxonomy.csv` for spreadsheet review.

**Size:** 28 categories / 199 services — 9 core (71 services), 11 growth (72), 8 specialist (56).

---

## 1. The shape is decided by the schema, not by taste

`home.ServiceCategory` has exactly one field, `name`, and it is the primary key. There is no slug, no icon, no order column, and no parent pointer. `home.Service` points at it with a foreign key. So the only taxonomy this database can hold is a flat list of categories each with its own services — which is what this is. Two levels, no more.

Two consequences worth knowing before you cut this into the app:

1. **`Service.name` is unique across the whole table, not per category.** "Cleaning" cannot exist both under Plumbing and under Cleaning. Every name in the catalogue is therefore written the way a customer would search for the job, and where a word would otherwise repeat it is qualified: *Water Tank & Stand Installation* (Plumbing) versus *Water Tank Cleaning* (Cleaning); *Borehole Pump Servicing* (Plumbing) versus *Solar Water Pump Installation* (Solar & Inverters). The loader reports any name it had to move rather than creating a duplicate.
2. **The public endpoints sort alphabetically.** `/api/v1/services/categories/` returns only categories that have at least one active service, ordered by `name`, so the customer app shows *Air Conditioning* first and *Welding & Metalwork* last — not the demand order below. If you want core trades first in the picker, either keep a display order in the app or we add an `ordering` integer field to `ServiceCategory` (that is a migration; say the word and I will do it).

---

## 2. How the catalogue was chosen

A category is **core** when it appears as a top-level category on most Nigerian artisan marketplaces *and* it is a recognised Nigerian construction or household trade. Everything after that is **growth** (real demand, smaller average ticket or seasonal) or **specialist** (a different kind of purchase — an event, a service contract, a person rather than a repair).

The ordering inside each tier is the order a Lagos customer is most likely to need them, cross-checked against four independent kinds of evidence:

| Evidence | What it says |
|---|---|
| **Fixgent.ng** — 18 categories / 140 services | The broadest Nigerian home-services catalogue found. Confirms repairs, cleaning, painting, building, appliances, power, security, pest control and moving as separate consumer-facing categories. |
| **CraftRanked** — 5 categories / 29 types | "Home & Utility" = electricians, plumbers, AC technicians, cleaners, generator repair, laundry. |
| **HandLancer** — 12 trades | Plumber, electrician, carpenter, bricklayer, painter, welder, mechanic, cleaner, gardener, tailor, barber, handyman; plus masonry, AC, auto repair, haulage. |
| **Zuno.ng** — Lagos price list | Top sellers: AC repair (from ₦20 000), plumbing (₦15 000), electrical (₦17 000), cleaning (₦10 000), washing-machine repair. |
| **Enugu artisan study (Springer, 2025)** — 466 artisans | Trade mix: masons 30 %, carpenters 25 %, electricians 20 %, plumbers 15 %, painters 5 %, tilers 2.5 %. This is why Building & Masonry, Carpentry and Tiling sit high despite being less visible than AC repair. |
| **Osogbo–Iwo survey** — 5 201 registered artisans | Carpenters 2 185, masons 1 190, electricians 705, iron benders 455, plumbers 375, painters 291. Supply-side weighting. |
| **SEforALL / Lagos genset survey (2024)** | 72 % of Lagos households own at least one generator — the single strongest argument for putting Generators & Power in the core. |
| **Jiji.ng repair listings** (Lagos, Abuja, Ibadan) | Live asking prices for washing-machine repair (₦9 999–10 000), AC repair and installation, gas-cooker repair, fridge/freezer and cold room, generator servicing, solar/inverter installation, CCTV, laptop and phone repair, TV repair, POP ceilings. |
| **Nairaland "50 artisan jobs in Lagos"** | Plumbing, electrical, carpentry and furniture, tiling, welding and fabrication, generator repair, upholstery and interior, laundry, water-pump installation. |
| **NBS Residential Energy Survey 2024** | Only 58.2 % of households connected to grid supply and 4.8 % with solar at home — the gap is what generators and inverters fill. |

Services under each category are the jobs that actually appear in those listings, written in customer language. Nothing here is priced: price lives in `ArtisanService.starting_price`, which is the artisan's number, not the catalogue's.

---

## 3. The catalogue

### Plumbing  ·  `core`

*Water supply is private in most Nigerian buildings (boreholes and tanks), so plumbing is continuous demand rather than an occasional repair.*

| Service | What the job is |
|---|---|
| Pipe Installation & Replacement | New pipe runs and replacement of old galvanised or PVC lines in homes, shops and offices. |
| Leak Detection & Repair | Tracing and fixing leaking pipes, joints, taps and hidden wall leaks. |
| Tap, Mixer & Shower Installation | Fitting and replacing kitchen taps, mixers, shower sets and flexible hoses. |
| Toilet & Bathroom Fitting | Installation and repair of WC, cisterns, basins and complete bathroom suites. |
| Water Tank & Stand Installation | Overhead and ground tanks, wooden or metal stands, and tank inlet and outlet plumbing. |
| Pump & Pressure Machine Installation | Submersible and surface pumps: installation, replacement and repair. |
| Drain & Sewage Clearing | Blocked drains, soakaways and sewage lines cleared and tested. |
| Water Heater Installation & Repair | Instant and storage water heaters fitted, serviced and repaired. |
| Water Treatment & Filtration | Filters, softeners and reverse-osmosis units fitted to a private water supply. |
| Borehole Pump Servicing | Pull, service and refit a borehole pump when water pressure drops. |

### Electrical  ·  `core`

*Grid supply is unreliable and voltage fluctuates, so wiring, changeovers and fault-finding are everyday jobs.*

| Service | What the job is |
|---|---|
| House Wiring & Rewiring | Full wiring of new buildings and rewiring of old or unsafe installations. |
| Socket, Switch & Light Fitting | Adding, moving or replacing sockets, switches and light fittings. |
| Distribution Board Installation | Consumer unit or fuse box installation, replacement and circuit labelling. |
| Electrical Fault Finding | Tracing tripping, sparks, burning smells and dead circuits to the source. |
| Meter & Changeover Installation | Prepaid meter fitting and generator-to-grid changeover switches. |
| Compound & Street Light Installation | Floodlights, security lights and solar or mains compound lighting. |
| Stabiliser & UPS Installation | Voltage stabilisers and UPS units sized and installed for the load. |
| Ceiling Fan Installation | Fitting and repairing ceiling and wall fans. |

### Air Conditioning  ·  `core`

*Hot, humid climate plus heavy use means servicing and gas refills are a recurring, high-frequency job.*

| Service | What the job is |
|---|---|
| AC Installation (Split & Window) | Fitting new split or window units with brackets, piping and drainage. |
| AC Servicing & Gas Refill | Wash, deep clean and regas an air conditioner that is no longer cooling. |
| AC Repair & Fault Diagnosis | Diagnosing and repairing units that trip, leak water or make noise. |
| AC Relocation (Dismantle & Reinstall) | Dismantle, transport and reinstall a unit when moving home or office. |
| Cassette & Central AC Servicing | Servicing of cassette, ducted and multi-split systems. |
| Refrigerator Repair | Domestic fridges and freezers that stop cooling, leak or run constantly. |
| Cold Room & Freezer Repair | Cold rooms, display freezers and ice-block machines serviced and repaired. |

### Generators & Power  ·  `core`

*Around seven in ten Lagos households own a generator, so servicing and repair is one of the largest recurring spend lines in a Nigerian home.*

| Service | What the job is |
|---|---|
| Generator Servicing & Oil Change | Routine service: oil, filters, plugs and a load test. |
| Petrol Generator Repair | Starting faults, surging, smoking and carburettor problems on petrol sets. |
| Diesel Generator Repair | Diagnosis and repair of diesel sets, including fuel and injection faults. |
| Generator Installation & Changeover | Positioning, wiring and changeover installation for a new set. |
| Alternator & AVR Repair | Rewinding, brushes and automatic voltage regulator faults. |
| Control Panel & ATS Installation | Automatic transfer switch and control panel installation and repair. |
| Generator House Ventilation | Exhaust routing and ventilation so a set can run safely in an enclosed space. |

### Cleaning  ·  `core`

*The highest-frequency repeat purchase in home services; every marketplace reviewed leads with it.*

| Service | What the job is |
|---|---|
| Standard Home Cleaning | Sweeping, mopping, surfaces, kitchen and bathrooms for a whole home. |
| Deep Cleaning | Top-to-bottom clean including inside cupboards, behind furniture and grout. |
| Post-Construction Cleaning | Clearing dust, cement splash and paint spots after building or renovation work. |
| Office & Shop Cleaning | Scheduled cleaning for offices, shops and small commercial spaces. |
| Sofa, Rug & Mattress Cleaning | Shampoo and extraction cleaning for upholstery, rugs and mattresses. |
| Window & Glass Cleaning | Interior and exterior glass, frames and louvres. |
| Water Tank Cleaning | Drain, scrub and disinfect a storage tank, then refill. |
| Move-In / Move-Out Cleaning | A full clean before furniture arrives or after a tenant leaves. |
| Short-Let Turnaround Cleaning | Between-guest cleaning for short-let and Airbnb apartments. |

### Carpentry & Furniture  ·  `core`

*Carpenters are the second largest construction trade by population in Nigerian studies, and fitted furniture is bought for almost every new home.*

| Service | What the job is |
|---|---|
| Custom Furniture Making | Beds, wardrobes, TV consoles and dining sets built to the client's size. |
| Fitted Wardrobe Installation | Built-in wardrobes and walk-in closets measured, made and fitted. |
| Kitchen Cabinet Installation | Kitchen units, worktops and drawer systems made and installed. |
| Door Hanging & Repair | Hanging new doors and repairing sagging, sticking or damaged ones. |
| Furniture Assembly | Assembling flat-pack and imported furniture on site. |
| Wood Polishing & Restoration | Sanding, staining and re-polishing tired wooden furniture. |
| Office Fit-Out Carpentry | Reception desks, workstations and partition carpentry for offices. |

### Painting  ·  `core`

*Painters are a smaller trade by population but almost every handover, let and repair ends in a repaint.*

| Service | What the job is |
|---|---|
| Interior Painting | Walls and ceilings prepared, primed and painted. |
| Exterior Painting | Weatherproof coating for outside walls, fences and balconies. |
| Texture & Screeding Finish | Decorative textured finishes and smooth screeded walls. |
| Wallpaper Installation | Hanging, matching and trimming wallpaper. |
| POP Ceiling Painting | Painting and touching up POP and plasterboard ceilings. |
| Spray Painting (Doors & Metal) | Spray finishing for doors, gates, railings and metal frames. |
| Painting Inspection & Quotation | Site visit and a written scope before a painting job starts. |

### Tiling & Flooring  ·  `core`

*Tiling is part of almost every Nigerian fit-out, from bathrooms to whole ground floors.*

| Service | What the job is |
|---|---|
| Floor Tiling | Laying floor tiles with levelling, cutting and grouting. |
| Wall Tiling | Bathroom, kitchen and feature wall tiling. |
| Marble & Granite Fitting | Cutting and fitting marble, granite and porcelain slabs. |
| Terrazzo & Epoxy Flooring | Seamless terrazzo or epoxy floors for homes, shops and workshops. |
| Interlocking Paving Stones | Interlocking stones laid for compounds, driveways and walkways. |
| Wood & Laminate Flooring | Laminate, vinyl and engineered wood floor installation. |
| Tile Removal & Replacement | Removing damaged or outdated tiles and replacing them. |

### Phone & Laptop Repair  ·  `core`

*Phone and laptop repair is a large informal trade in every Nigerian city, with walk-in demand and fast turnaround.*

| Service | What the job is |
|---|---|
| Phone Screen Replacement | Replacing cracked or dead phone screens with a warranty on the part. |
| Phone Battery & Charging Port Repair | Battery swaps, charging port replacement and charging fault diagnosis. |
| Phone Software & Flashing | Software repair, flashing and account or activation issues. |
| Laptop Screen Replacement | Laptop screen replacement for cracked, flickering or blank displays. |
| Laptop Battery & Charging Repair | Battery replacement, DC jack and charger circuit repair. |
| Laptop Keyboard & Hinge Repair | Keyboard replacement and hinge or casing repair. |
| Data Recovery & Backup | Recovering files from a laptop or phone that will not boot. |
| TV Repair | Television repair for no picture, no sound and backlight faults. |
| Printer & Office Equipment Repair | Printer, scanner and photocopier servicing and repair. |

### Solar & Inverters  ·  `growth`

*Solar and inverter adoption is climbing fast in Lagos, and every installation needs an installer and later servicing.*

| Service | What the job is |
|---|---|
| Solar Panel Installation | Panels mounted, wired and commissioned for a home or small business. |
| Inverter & Battery Installation | Sizing, mounting and wiring an inverter and battery bank. |
| Solar System Servicing | Cleaning panels, checking connections and testing battery health. |
| Battery Replacement | Testing and replacing failed or degraded batteries in an existing system. |
| Solar Water Pump Installation | Solar-powered borehole and irrigation pump installation. |
| Solar Street Light Installation | Solar street and compound lights installed and configured. |
| Energy Audit & System Sizing | Load audit and a written system size before buying equipment. |

### Building & Masonry  ·  `growth`

*Masons and bricklayers are the largest single construction trade group in Nigerian studies; renovation work is constant in Lagos.*

| Service | What the job is |
|---|---|
| Blockwork & Bricklaying | Walls built, extended or repaired in block or brick. |
| Plastering & Rendering | Internal plaster and external render finished level. |
| Screeding & Concrete Work | Floor screeding, concrete slabs and small structural works. |
| Bathroom & Kitchen Remodelling | Strip out and rebuild of a bathroom or kitchen, including wet areas. |
| House Extension & Renovation | Extensions, partitioning and general renovation of an existing building. |
| Boundary Wall & Fence Building | Perimeter walls, fences and copings built or repaired. |
| Site Clearing & Excavation | Clearing, levelling and digging in preparation for building work. |

### Roofing  ·  `growth`

*The rainy season turns any roof defect into an emergency, and re-roofing is a high-value job.*

| Service | What the job is |
|---|---|
| Roof Installation | Stone-coated, aluminium or long-span roofing sheets installed. |
| Roof Leak Repair | Finding and sealing leaks at sheets, ridges and flashings. |
| Roof Replacement & Re-Roofing | Removing an old roof and installing a new one. |
| Ceiling Waterproofing | Treating and sealing ceilings and decks against water ingress. |
| Damp Proofing | Treating rising damp and damp patches on internal walls. |
| Gutter Installation & Repair | Fitting and repairing gutters and downpipes. |

### Welding & Metalwork  ·  `growth`

*Gates, burglary proofing and railings are on nearly every Nigerian building, and welders are a recognised trade.*

| Service | What the job is |
|---|---|
| Gate Fabrication & Repair | Fabricating new gates and repairing or re-hanging existing ones. |
| Burglary Proof Fabrication | Window and door security grilles fabricated and fitted. |
| Handrail & Balustrade Fabrication | Staircase and balcony handrails made and installed. |
| Metal Door & Frame Fabrication | Steel doors and frames fabricated to an opening. |
| Canopy & Carport Fabrication | Carports, canopies and roof steelwork fabricated and erected. |
| Welding Repairs On Site | On-site welding of broken metalwork, frames and fittings. |

### Aluminium & Glass  ·  `growth`

*Aluminium windows and sliding doors are the default choice in Nigerian residential builds, and screens need replacing often.*

| Service | What the job is |
|---|---|
| Aluminium Window Installation | Casement and sliding aluminium windows measured, made and fitted. |
| Sliding Door Installation | Sliding and folding aluminium doors installed and adjusted. |
| Glass Partition & Shopfront | Shopfronts, office partitions and frameless glass work. |
| Window Net & Insect Screen Repair | Mosquito net, insect mesh and window screen replacement. |
| Shower Cubicle & Glass Fitting | Shower enclosures, glass screens and mirrors fitted. |
| Aluminium Repair & Servicing | Repairing runners, rollers, locks and bent frames. |

### Ceilings & POP  ·  `growth`

*POP ceilings are standard in Nigerian residential finishing and a common upgrade in rented property.*

| Service | What the job is |
|---|---|
| POP Ceiling Design & Installation | Plaster of Paris ceilings designed and installed. |
| PVC Ceiling Installation | PVC panel ceilings fitted for bathrooms, kitchens and shops. |
| Gypsum & Drywall Partitioning | Stud partitions and drywall rooms built and finished. |
| Ceiling Board Replacement | Replacing sagging, stained or damaged ceiling boards. |
| Cove & Bulkhead Finishing | Cove lighting and bulkhead details built into a ceiling. |
| Acoustic Wall & Soundproofing | Acoustic board and soundproofing work for studios and offices. |

### Upholstery & Interior  ·  `growth`

*Re-upholstery is cheaper than replacing furniture, so it sells steadily; decor work is seasonal but high value.*

| Service | What the job is |
|---|---|
| Sofa Re-Upholstery | Stripping and re-covering sofas in a new fabric. |
| Chair Re-Upholstery | Dining, office and reception chairs re-covered and re-foamed. |
| Curtain Making & Installation | Curtains measured, made and hung with the right track. |
| Blinds Installation | Venetian, roller and vertical blinds measured and fitted. |
| Headboard & Bed Frame Upholstery | Padded headboards and upholstered bed frames made to order. |
| Wall Panel & Cladding Installation | Wall panels, cladding and feature walls installed. |
| Interior Decor Styling | Room styling with advice on layout, colour and soft furnishing. |

### Pest Control  ·  `growth`

*A humid climate and dense housing make fumigation a routine purchase for homes, offices and food premises.*

| Service | What the job is |
|---|---|
| Residential Fumigation | Full fumigation of a home, flat or duplex. |
| Commercial Fumigation | Offices, restaurants, hotels and schools treated and certified. |
| Bed Bug Treatment | Targeted treatment for bed bugs in mattresses, frames and skirting. |
| Termite Treatment | Termite control for structures and pre-treatment before building. |
| Rodent Control | Rats and mice controlled with baiting and entry-point sealing. |
| Mosquito & Insect Control | Indoor and compound mosquito and insect treatment. |
| Post-Construction Fumigation | Fumigation before moving into a newly built or renovated property. |

### Laundry & Garment Care  ·  `growth`

*Wash-and-fold and dry cleaning are repeat weekly purchases, and the pickup model suits Lagos traffic.*

| Service | What the job is |
|---|---|
| Laundry Pickup & Delivery | Wash, dry and fold with collection and return. |
| Dry Cleaning | Dry cleaning for suits, native wear, dresses and delicate fabrics. |
| Ironing Only | Pressing service for clothes washed at home. |
| Corporate & Uniform Laundry | Recurring laundry for offices, hotels and uniforms. |
| Curtain & Duvet Cleaning | Large-item cleaning for curtains, duvets and bedspreads. |
| Shoe & Bag Cleaning | Cleaning, polishing and repair for shoes, bags and luggage. |

### Moving & Errands  ·  `growth`

*Relocation is frequent in Lagos, and buyers want labour plus a vehicle in one booking.*

| Service | What the job is |
|---|---|
| House Moving (Loading & Offloading) | Packing, loading, moving and offloading a home. |
| Truck & Van Hire With Driver | A vehicle and driver for a move or a large delivery. |
| Office Relocation | Packing and relocating an office out of hours. |
| Furniture Transport | Moving a single item such as a fridge, sofa or bed. |
| Junk & Rubbish Removal | Clearing out and disposing of unwanted items. |
| Dispatch & Delivery Runs | Same-day pickup and delivery across the city. |
| Packing & Unpacking | Packing before a move and unpacking at the destination. |

### Security Systems  ·  `growth`

*CCTV and electric fencing are mainstream in Lagos estates, and every install needs maintenance.*

| Service | What the job is |
|---|---|
| CCTV Installation | Cameras, recorder and remote viewing set up and explained. |
| CCTV Repair & Maintenance | Fixing blind spots, dead cameras and failed recording. |
| Electric Fence Installation | Perimeter electric fencing installed with energiser and alarm. |
| Gate Automation | Automatic gate motors installed, serviced and repaired. |
| Intercom & Video Doorbell | Intercom and video doorbell installation. |
| Alarm System Installation | Intruder alarms with sensors and phone alerts. |
| Locksmith Services | Lock changes, lockouts and safe or cabinet locks. |

### Tailoring  ·  `specialist`

*Bespoke tailoring is a large Nigerian trade with steady demand around events and ceremonies.*

| Service | What the job is |
|---|---|
| Native Wear Tailoring | Agbada, kaftan, senator and other native styles made to measure. |
| English Wear Tailoring | Shirts, trousers, dresses and suits made to measure. |
| Bridal & Aso-Ebi Outfits | Bridal gowns and matching family or party outfits produced to a theme. |
| Alterations & Repairs | Taking in, letting out and repairing existing clothes. |
| Uniform & Corporate Wear | Uniforms and branded workwear produced for a team. |
| Embroidery & Monogramming | Names, logos and monograms embroidered onto garments. |

### Beauty & Grooming  ·  `specialist`

*Barbers and stylists already work house to house in Nigeria; the marketplace formalises a habit that exists.*

| Service | What the job is |
|---|---|
| Home Barbering | A barber comes to the house for a cut and lineup. |
| Hair Styling & Braiding | Braids, cornrows, twists and styling done at home. |
| Wig Installation & Styling | Wig installation, styling and revamping. |
| Makeup Artistry | Event, bridal and photoshoot makeup at location. |
| Gele Tying | Traditional gele tying for weddings and ceremonies. |
| Manicure & Pedicure | Home manicure, pedicure and nail art. |
| Lashes & Brows | Lash extensions, lifts and brow shaping. |
| Bridal Beauty Package | Hair, makeup and gele planned and delivered on the wedding day. |

### Catering  ·  `specialist`

*Event catering and small chops are a large Nigerian informal business, with strong repeat demand.*

| Service | What the job is |
|---|---|
| Event Catering | Full catering for weddings, birthdays and corporate events. |
| Small Chops & Pastries | Small chops and pastry platters made and delivered. |
| Private Chef / Home Cook | A cook for a day, a week or a dinner at home. |
| Cake Baking & Desserts | Celebration cakes and desserts baked to order. |
| Cocktails & Drinks Service | Cocktail, mocktail and drinks service for events. |
| Meal Prep & Weekly Food Delivery | A week of prepared meals cooked and delivered. |

### Events & Decor  ·  `specialist`

*Weddings, burials and naming ceremonies support a large rental, decor and staffing economy.*

| Service | What the job is |
|---|---|
| Event Planning & Coordination | Planning and running an event on the day. |
| Event Decoration & Stage Design | Hall and stage decor, backdrop and floral work. |
| Canopy, Chair & Table Rental | Rental, delivery and setup of canopies, tables and chairs. |
| DJ Services | DJ, sound and lighting for an event. |
| MC & Event Host | An MC to run the event programme. |
| Ushers & Event Staff | Ushers, waiters and support staff supplied for an event. |
| Bouncy Castle & Kids Entertainment | Bouncy castle, games and children's entertainment hire. |
| Photo Booth Rental | Photo booth hire with props and instant prints. |

### Photography & Video  ·  `specialist`

*Every ceremony is photographed, and the same freelancers serve product and studio work between events.*

| Service | What the job is |
|---|---|
| Event Photography | Stills coverage of a ceremony, party or corporate event. |
| Event Videography | Video coverage with a highlight edit. |
| Studio Portraits | Studio or outdoor portrait session. |
| Product Photography | Product shots for online listings and catalogues. |
| Wedding Coverage | Full-day wedding photo and video coverage. |
| Drone Coverage | Aerial photo and video coverage of an event or property. |
| Video Editing | Editing existing footage into a finished piece. |

### Auto Services  ·  `specialist`

*Mobile mechanics and car care are already sold door to door; the category converts existing informal demand.*

| Service | What the job is |
|---|---|
| Mobile Mechanic | Diagnosis and repair done at the customer's location. |
| Auto Electrician | Battery, alternator, starter and wiring faults on a vehicle. |
| Car Wash & Detailing | Wash, interior clean, polish and detailing. |
| Panel Beating & Spraying | Dent removal, panel beating and respray. |
| Tyre Service & Wheel Balancing | Tyre fitting, balancing and puncture repair. |
| Oil Change & Routine Service | Oil, filters and fluids changed on a schedule. |
| Windscreen Replacement | Windscreen and side glass replacement. |

### Garden & Outdoor  ·  `specialist`

*Compound upkeep is a subscription habit in estates, and pools need specialist attention.*

| Service | What the job is |
|---|---|
| Compound Cleaning | Sweeping, weeding and waste removal for a compound. |
| Lawn Mowing | Regular or one-off lawn cutting and edging. |
| Tree Trimming & Removal | Pruning and safe removal of trees that overhang a building. |
| Landscaping Design | Garden design, planting and hard landscaping. |
| Fence Installation & Repair | Wire, wood and mesh fencing installed or repaired. |
| Swimming Pool Cleaning & Repair | Pool cleaning, chemical balancing, pump and filter repair. |
| Gutter Cleaning | Clearing gutters and downpipes before the rains. |

### Home & Domestic Staff  ·  `specialist`

*Households routinely hire help, nannies and tutors informally; a vetted marketplace route is a clear gap.*

| Service | What the job is |
|---|---|
| House Help / Housekeeper | A live-in or daily housekeeper, interviewed and referenced. |
| Nanny & Childcare | Nanny or babysitting cover, day or live-in. |
| Elderly Care & Companionship | Care and companionship for an elderly relative at home. |
| Home Tutor | A tutor for school subjects at home. |
| Driver for Hire | A vetted driver, full time or occasional. |
| House Sitting | A trusted person to stay while the family travels. |
| Pet Sitting & Dog Walking | Feeding, walking and care for pets at home. |


---

## 4. What I deliberately left out

These exist on Jiji or Fixgent but are not a two-level fit for a home-artisan marketplace, so they are excluded rather than half-supported:

- **Legal services, travel agents, recruitment** — professional engagements with their own licensing and paperwork; they do not run through the seven-phase booking flow.
- **Classes & courses, online & remote work** (graphic design, video editing, virtual assistant, data entry, bookkeeping) — these are not location-based artisan jobs, and the platform's escrow and phase tracking assume physical delivery.
- **Printing, manufacturing** — B2B production work.
- **Wedding venues** — a venue hire, not a service provider.
- **Vehicle hire / chauffeur and airport transfer** — I kept "Driver for Hire" under Home & Domestic Staff but excluded fleet and airport transfer because they need a different scheduling model from a per-job booking.

If any of these matter to you later, they are additions to `CATEGORIES` and nothing else — the loader will create them on the next run.

---

## 5. How to load it

```powershell
# Windows PowerShell, from the backend folder, with your virtualenv active
python manage.py seed_service_taxonomy --dry-run     # show what would change, write nothing
python manage.py seed_service_taxonomy               # create categories + services
python manage.py seed_service_taxonomy --tier core    # launch with the 9 core trades only
python manage.py seed_service_taxonomy --prune       # also soft-disable services the catalogue dropped
```

What the loader guarantees:

- **Idempotent.** `get_or_create` on `ServiceCategory.name` and `Service.name`. Running it twice gives `Created 0 categories / 0 services; 0 services updated, 199 already correct`.
- **Repairs drift instead of duplicating.** If a name exists under the wrong category, or its description is stale, or it was deactivated by hand, the next run moves/restores it and prints what it moved.
- **Never deletes.** `--prune` sets `is_active=False` only, because bookings, artisan services and ratings reference these rows.
- **One transaction.** A half-loaded catalogue can never be visible to customers; a dry run rolls back.
- **Re-activates.** A service that an admin switched off is switched back on by a normal run — use `is_active=False` in the database only if you mean it permanently, in which case remove it from `service_taxonomy.py` as well.

---

## 6. Verification actually run

| Check | Result |
|---|---|
| `makemigrations --check` | `No changes detected` — no model change, data-only |
| Fresh SQLite, `migrate` then `--dry-run` | "Would create 28 categories / 199 services", database still `0 0` |
| Real run | `28 199` rows |
| Immediate re-run | `Created 0 categories / 0 services … 199 already correct` |
| Hand-mangled row (wrong category, stale text, inactive) | Repaired: moved `Cleaning -> Plumbing`, description restored, `is_active` back to `True` |
| `--prune` with an injected stale row | `Disabled 1 stale services` — row kept, `is_active=False`, 199 active remain |
| `/api/v1/services/categories/` | `200`, 28 categories |
| `/api/v1/services/service_cat/?category_name=Plumbing` | `200`, 10 rows, all `category_name: "Plumbing"` |
| `home.test_service_taxonomy` | **7 tests, OK** |
| `manager home services` suites | 134 tests, the only 2 failures are `home.test_staff_admin_mfa` *Referrer-Policy* assertions (`same-origin` vs `no-referrer`), which **also fail on the previously shipped zip** — pre-existing, unrelated to this change |
| Clean extraction of `Artisan_backend_corrected.zip` into an empty folder, fresh SQLite | `makemigrations --check` clean; dry run → real run (`28 / 199`) → re-run (`199 already correct`); `manager` + `home.test_service_taxonomy` = **89 tests, OK** |
| Archive contents | 0 `__pycache__`, `.pyc` or `celerybeat-schedule` entries; the only differences from the previously shipped zip are the 7 new files above |

---

## 7. Decisions for you

1. **Launch set:** all 28 categories (my recommendation — an empty category costs nothing and the picker only shows categories with active services), or `--tier core` for the 9 trades first?
2. **Ordering in the app:** alphabetical (today, needs nothing) or demand order (needs a small `ordering` field and a migration)?
3. **Prices:** the catalogue carries none. If you want a "typical starting price" hint in the customer app, that should come from real `ArtisanService` rows, not a hardcoded number — tell me and I will build it from live data.
4. **`MAX_ARTISAN_SERVICES = 3`** in `frontend/views.py` still caps each artisan at three services. With 199 services in the catalogue that cap is what limits how well a plumber can describe his full range. I left it alone because it is a product rule you set, not a bug — say the word and I will raise it.
