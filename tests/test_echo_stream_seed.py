from charms_seed_echo_stream import EchoStreamSeed

from charms_core.chunk import Chunk
from charms_core.types import Modality


def _chunk(seq: int, text: str) -> Chunk:
    return Chunk(stream_id="s1", seq=seq, modality=Modality.TEXT, text=text)


async def test_stream_lifecycle() -> None:
    seed = EchoStreamSeed()
    await seed.stream_open("s1")

    first = await seed.stream_chunk("s1", _chunk(0, "hello "))
    second = await seed.stream_chunk("s1", _chunk(1, "world"))
    assert first is not None and first.text == "HELLO "
    assert second is not None and second.text == "WORLD"

    final = await seed.stream_close("s1")
    assert final is not None
    assert final.text == "HELLO WORLD"


async def test_final_empty_marker_produces_nothing() -> None:
    seed = EchoStreamSeed()
    await seed.stream_open("s1")
    marker = Chunk(stream_id="s1", seq=0, modality=Modality.TEXT, is_final=True)
    assert await seed.stream_chunk("s1", marker) is None
    final = await seed.stream_close("s1")
    assert final is not None and final.text == ""


async def test_streams_are_isolated() -> None:
    seed = EchoStreamSeed()
    await seed.stream_open("a")
    await seed.stream_open("b")
    await seed.stream_chunk("a", _chunk(0, "aa"))
    await seed.stream_chunk("b", _chunk(0, "bb"))
    assert (await seed.stream_close("a")).text == "AA"  # type: ignore[union-attr]
    assert (await seed.stream_close("b")).text == "BB"  # type: ignore[union-attr]
