from __future__ import annotations

from translations_verifier.models import (
    ArbDocument,
    OperationType,
    TranslationOperation,
)


def diff_translations(
    *,
    source: ArbDocument,
    before: ArbDocument | None,
    after: ArbDocument,
    locale: str,
) -> list[TranslationOperation]:
    before_messages = before.messages if before else {}
    keys = sorted(set(before_messages) | set(after.messages))
    operations: list[TranslationOperation] = []

    for key in keys:
        old_message = before_messages.get(key)
        new_message = after.messages.get(key)
        if old_message is None and new_message is not None:
            operation = OperationType.INSERTED
        elif old_message is not None and new_message is None:
            operation = OperationType.DELETED
        elif (
            old_message is not None
            and new_message is not None
            and (
                old_message.value != new_message.value
                or old_message.metadata != new_message.metadata
            )
        ):
            operation = OperationType.CHANGED
        else:
            continue

        operations.append(
            TranslationOperation(
                operation=operation,
                key=key,
                path=after.path,
                locale=locale,
                source=source.messages.get(key),
                before=old_message,
                after=new_message,
            )
        )

    return operations
