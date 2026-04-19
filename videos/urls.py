from django.urls import path
from . import views

urlpatterns = [
    # ── Frontend pages ──
    path('',               views.player_page,         name='player'),
    path('player/',        views.player_page),
    path('upload/',        views.upload_page,         name='upload'),
    path('analytics/',     views.analytics_page,      name='analytics'),
    path('channels/',      views.channels_manage_page, name='channels_manage'),
    path('trending/',      views.trending_page,       name='trending'),
    path('saved/',         views.saved_page,          name='saved'),
    path('history/',       views.history_page,        name='history'),
    path('playlists/',     views.playlists_page,      name='playlists'),
    path('moments/',       views.moments_page,        name='moments'),
    path('notifications/', views.notifications_page,  name='notifications'),
    path('admin-panel/',             views.user_management_page,    name='user_management'),
    path('admin-panel/categories/',  views.categories_manage_page,  name='categories_manage'),
    path('admin-panel/commands/',    views.admin_commands_page,     name='admin_commands'),
    path('admin-panel/storage/',     views.storage_dashboard_page,  name='storage_dashboard'),
    path('api/admin/commands/run/',  views.admin_commands_run),
    path('faces/',                       views.faces_page,          name='faces'),
    path('faces/<int:identity_id>/',     views.face_identity_page,  name='face_identity'),
    path('speakers/',                    views.speakers_page,        name='speakers'),
    path('speakers/<int:speaker_id>/',   views.speaker_identity_page, name='speaker_identity'),

    # ── Auth / onboarding ──
    path('register/',      views.register_page,      name='register'),
    path('setup-channel/', views.setup_channel_page, name='setup_channel'),

    # ── Content pages ──
    path('watch/<uuid:video_id>/',            views.watch_page,           name='watch'),
    path('watch/<uuid:video_id>/xray/',       views.video_xray_page,      name='video_xray'),
    path('embed/<uuid:video_id>/',            views.embed_page,           name='embed'),
    path('channel/<slug:slug>/',              views.channel_page,         name='channel'),
    path('category/<slug:slug>/',             views.category_page,        name='category'),
    path('playlist/<uuid:playlist_id>/',      views.playlist_detail_page, name='playlist_detail'),

    # ── API: Health ──
    path('api/health/', views.health_check),

    # ── API: Search ──
    path('api/search/suggest/', views.search_suggest),
    path('api/search/scoped/',  views.scoped_search_api),

    # ── API: Channels ──
    path('api/channels/',                                               views.channel_list),
    path('api/channels/<uuid:channel_id>/',                             views.channel_detail_by_id),
    path('api/channels/<uuid:channel_id>/editors/',                     views.channel_editors),
    path('api/channels/<uuid:channel_id>/editors/<int:user_id>/',       views.channel_editor_remove),
    path('api/channels/<slug:slug>/subscribe/',                         views.toggle_subscribe),
    path('api/channels/<slug:slug>/links/',                             views.channel_links),
    path('api/channels/<slug:slug>/',                                   views.channel_detail),

    # ── API: Channel Links ──
    path('api/channel-links/<int:link_id>/', views.channel_link_detail),

    # ── Named Places ──
    path('named-places/',                    views.named_places_page, name='named_places'),
    path('places/<slug:slug>/',              views.place_detail_page, name='place_detail'),
    path('api/named-places/',                views.named_place_list),
    path('api/named-places/<int:place_id>/', views.named_place_detail),

    # ── API: Categories ──
    path('api/categories/', views.category_list),
    path('api/categories/<int:category_id>/', views.category_detail),

    # ── API: Moment Categories ──
    path('api/moment-categories/', views.moment_category_list),
    path('api/moment-categories/<int:cat_id>/', views.moment_category_detail),


    # ── API: Videos ──
    path('api/videos/',                                    views.video_list),
    path('api/videos/upload/',                             views.video_upload),
    path('api/videos/<uuid:video_id>/',                    views.video_detail),
    path('api/videos/<uuid:video_id>/view/',               views.record_view),
    path('api/videos/<uuid:video_id>/status/',             views.video_status),
    path('api/videos/<uuid:video_id>/reprocess/',          views.reprocess_video),
    path('api/videos/<uuid:video_id>/upscale/presets/',     views.video_upscale_presets),
    path('api/videos/<uuid:video_id>/upscale/',             views.video_upscale_start),
    path('api/videos/<uuid:video_id>/stream/',             views.stream_playlist),
    path('api/videos/<uuid:video_id>/download/',           views.video_download),
    path('api/videos/<uuid:video_id>/thumbnail/',          views.video_update_thumbnail),
    path('api/videos/<uuid:video_id>/like/',               views.toggle_like),
    path('api/videos/<uuid:video_id>/save/',               views.toggle_save),
    path('api/videos/<uuid:video_id>/history/',            views.delete_watch_history),
    path('api/videos/<uuid:video_id>/progress/',           views.update_watch_progress),
    path('api/videos/<uuid:video_id>/comments/',           views.comment_list_create),
    path('api/videos/<uuid:video_id>/chapters/',           views.chapter_list_create),
    path('api/videos/<uuid:video_id>/moments/',           views.moment_list_create),
    path('api/videos/<uuid:video_id>/end-screens/',        views.end_screen_list_create),
    path('api/videos/<uuid:video_id>/end-screens/<int:end_screen_id>/', views.end_screen_detail),

    # ── API: Subtitles ──
    path('api/videos/<uuid:video_id>/subtitles/',                         views.subtitle_list),
    path('api/videos/<uuid:video_id>/subtitles/upload/',                  views.subtitle_upload),
    path('api/videos/<uuid:video_id>/subtitles/regenerate/',              views.subtitle_regenerate),
    path('api/videos/<uuid:video_id>/subtitles/<int:subtitle_id>/',       views.subtitle_delete),
    path('api/videos/<uuid:video_id>/subtitles/<int:subtitle_id>/cues/', views.subtitle_cues),
    path('watch/<uuid:video_id>/subtitles/<int:subtitle_id>/edit/',      views.subtitle_editor_page, name='subtitle_editor'),
    path('watch/<uuid:video_id>/transcript/',                            views.transcript_editor_page, name='transcript_editor'),

    # ── API: Audio Tracks ──
    path('api/videos/<uuid:video_id>/audio-tracks/',          views.audio_track_list),
    path('api/videos/<uuid:video_id>/audio-tracks/extract/',  views.audio_track_extract),

    # ── API: Video Frames (visual analysis) ──
    path('api/videos/<uuid:video_id>/frames/',          views.video_frames_list),
    path('api/videos/<uuid:video_id>/frames/analyze/',  views.video_frames_analyze),

    # ── API: Speaker Diarization ──
    path('api/videos/<uuid:video_id>/diarize/', views.run_diarization),

    # ── API: Face Recognition ──
    path('api/videos/<uuid:video_id>/faces/',                              views.video_faces_list),
    path('api/videos/<uuid:video_id>/faces/<int:identity_id>/remove/',    views.face_identity_remove_from_video),
    path('api/faces/list/',                                                  views.face_identity_list),
    path('api/faces/<int:identity_id>/tag/',                               views.face_identity_tag),
    path('api/faces/<int:identity_id>/merge/',                             views.face_identity_merge),
    path('api/faces/<int:identity_id>/rename/',                            views.face_identity_rename),
    path('api/faces/<int:identity_id>/nicknames/',                         views.face_identity_nicknames),
    path('api/faces/<int:identity_id>/nicknames/<int:nickname_id>/',       views.face_identity_nickname_delete),
    path('api/faces/<int:identity_id>/delete/',                            views.face_identity_delete),
    path('api/faces/crops/<int:face_id>/status/',                          views.face_set_status),
    path('api/faces/crops/<int:face_id>/set-thumbnail/',                   views.face_set_thumbnail),
    path('api/faces/cleanup-orphans/',                                     views.faces_cleanup_orphans),
    path('api/faces/<int:identity_id>/audio/',                             views.face_identity_audio_tab),
    path('api/segments/<int:segment_id>/set-speaker/',                     views.segment_set_speaker),

    # ── API: Speaker Identity ──
    path('api/videos/<uuid:video_id>/speakers/',            views.video_speakers_list),
    path('api/speakers/list/',                              views.speaker_list_api),
    path('api/speakers/<int:speaker_id>/rename/',           views.speaker_rename),
    path('api/speakers/<int:speaker_id>/set-role/',         views.speaker_set_role),
    path('api/speakers/<int:speaker_id>/link-face/',        views.speaker_link_face),
    path('api/speakers/<int:speaker_id>/suggestions/<int:suggestion_id>/accept/', views.speaker_suggestion_accept),
    path('api/speakers/<int:speaker_id>/suggestions/<int:suggestion_id>/reject/', views.speaker_suggestion_reject),
    path('api/speakers/<int:speaker_id>/merge/',            views.speaker_merge),
    path('api/speakers/<int:speaker_id>/delete/',           views.speaker_delete),

    # ── API: Comments ──
    path('api/comments/<int:comment_id>/',       views.comment_delete),
    path('api/comments/<int:comment_id>/like/',  views.toggle_comment_like),
    path('api/comments/<int:comment_id>/pin/',   views.pin_comment),

    # ── API: Chapters ──
    path('api/chapters/<int:chapter_id>/', views.chapter_detail),
    path('api/moments/<int:moment_id>/',  views.moment_detail),

    # ── API: Playlists ──
    path('api/playlists/',                                            views.playlist_list_create),
    path('api/playlists/<uuid:playlist_id>/',                         views.playlist_detail_api),
    path('api/playlists/<uuid:playlist_id>/videos/<uuid:video_id>/', views.playlist_video),

    # ── API: Notifications ──
    path('api/notifications/',       views.notification_list),
    path('api/notifications/read/',  views.mark_notifications_read),

    # ── API: Admin ──
    path('api/admin/users/',           views.user_management_api),
    path('api/admin/users/<int:user_id>/', views.user_management_toggle),

    # ── Media map (photos + videos with location) ──
    path('media/map/',                       views.media_map_page,       name='media_map'),

    # ── Photos (DAM) — pages ──
    path('photos/',                          views.photo_library_page,   name='photo_library'),
    path('photos/map/',                      views.photo_map_page,       name='photo_map'),
    path('photos/duplicates/',               views.photo_duplicates_page, name='photo_duplicates'),
    path('photos/upload/',                   views.photo_upload_page,    name='photo_upload'),
    path('photos/<uuid:photo_id>/',          views.photo_detail_page,    name='photo_detail'),

    # ── Photos (DAM) — API ──
    path('api/photos/',                             views.photo_list),
    path('api/media/map-markers/',                  views.media_map_markers),
    path('api/photos/map-markers/',                 views.photo_map_markers),
    path('api/photos/upload/',                      views.photo_upload),
    path('api/photos/bulk/',                        views.photo_bulk),
    path('api/photos/<uuid:photo_id>/',                                    views.photo_detail_api),
    path('api/photos/<uuid:photo_id>/status/',                             views.photo_status),
    path('api/photos/<uuid:photo_id>/archive/',                            views.photo_toggle_archive),
    path('api/photos/<uuid:photo_id>/upscale/presets/',                     views.photo_upscale_presets),
    path('api/photos/<uuid:photo_id>/upscale/',                             views.photo_upscale_start),
    path('api/photos/<uuid:photo_id>/faces/',                              views.photo_faces_list),
    path('api/photos/<uuid:photo_id>/faces/<int:identity_id>/remove/',     views.photo_face_remove),

    # ── Albums — pages ──
    path('albums/',                              views.albums_page,        name='albums'),
    path('albums/<uuid:album_id>/',              views.album_detail_page,  name='album_detail'),
    path('albums/shared/<str:share_token>/',     views.album_shared_page,  name='album_shared'),

    # ── Albums — API ──
    path('api/albums/',                              views.album_list),
    path('api/albums/<uuid:album_id>/',              views.album_detail),
    path('api/albums/<uuid:album_id>/photos/',       views.album_photos),

    # ── Events — pages ──
    path('events/',                views.events_page,         name='events'),
    path('events/upload/',         views.events_upload_page,  name='events_upload'),
    path('events/<slug:slug>/',    views.event_detail_page,   name='event_detail'),

    # ── Events — API ──
    path('api/events/',                        views.event_list_create),
    path('api/events/<int:event_id>/',         views.event_detail_api),
    path('api/events/<int:event_id>/search/',  views.event_search_api),
]
