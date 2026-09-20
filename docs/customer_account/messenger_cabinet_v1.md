# Messenger Customer Cabinet V1

Текущая продуктовая модель PRO-STOR: сайт остаётся анонимным, а Telegram и
MAX являются границей идентичности клиента.

## Статус web-account

`CUSTOMER_ACCOUNT_ENABLED=false` и `CUSTOMER_AUTH_MAX_ENABLED=false`.

Web Customer Account V1 не активирован. Его код и миграции остаются в проекте
как отложенная возможность:

**WEB CUSTOMER ACCOUNT V1 - DEFERRED / NOT ACTIVATED**

## Messenger cabinet

В Telegram и MAX доступны:

- «Мои заявки» — только заявки, связанные с конкретным provider user id;
- «Мои покупки» — только проведённые `Sale`/`SaleLine` клиента;
- детализация покупки;
- «Повторить покупку» через текущий preview.

Имя, username, телефон и display name не доказывают владение и не используются
для объединения клиентов. Покупки доступны только после явной связи
`CustomerIdentity` с карточкой DenisStock `Customer`.

Повторная покупка каждый раз заново проверяет текущую публичность, активность,
остаток и общий канонический customer-price resolver. Историческая
`SaleLine.unit_price` показывается только как история и никогда не становится
текущей ценой. Неизвестная цена отображается как «Цена уточняется», а не как
`0 ₽`.

Нажатие «Создать заявку» создаёт новую `CustomerRequest` после повторной
валидации. Заявка не является покупкой, резервом, оплатой или движением склада.

**MESSENGER CUSTOMER CABINET V1 - AUTHORITATIVE CURRENT PRODUCT DIRECTION**
