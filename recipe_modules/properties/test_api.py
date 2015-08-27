from recipe_engine import recipe_test_api

class PropertiesTestApi(recipe_test_api.RecipeTestApi):
  def __call__(self, **kwargs):
    ret = self.test(None)
    ret.properties.update(kwargs)
    return ret

  def generic(self, **kwargs):
    """
    Merge kwargs into a typical buildbot properties blob, and return the blob.
    """
    ret = self(
        blamelist='cool_dev1337@chromium.org,hax@chromium.org',
        blamelist_real=['cool_dev1337@chromium.org', 'hax@chromium.org'],
        buildbotURL='http://c.org/p/cr/',
        buildername='TestBuilder',
        buildnumber=571,
        mastername='chromium.testing.master',
        slavename='TestSlavename',
        workdir='/path/to/workdir/TestSlavename',
    )
    ret.properties.update(kwargs)
    return ret

  def scheduled(self, **kwargs):
    """
    Merge kwargs into a typical buildbot properties blob for a job fired off
    by a chrome/trunk svn scheduler, and return the blob.
    """
    ret = self.generic(
        branch='TestBranch',
        project='',
        repository='svn://svn-mirror.golo.chromium.org/chrome/trunk',
        revision='204787',
    )
    ret.properties.update(kwargs)
    return ret

  def git_scheduled(self, **kwargs):
    """
    Merge kwargs into a typical buildbot properties blob for a job fired off
    by a gitpoller/scheduler, and return the blob.
    """
    ret = self.generic(
        branch='master',
        project='',
        repository='https://chromium.googlesource.com/chromium/src.git',
        revision='c14d891d44f0afff64e56ed7c9702df1d807b1ee',
    )
    ret.properties.update(kwargs)
    return ret

  def tryserver(self, **kwargs):
    """
    Merge kwargs into a typical buildbot properties blob for a job fired off
    by a rietveld tryjob on the tryserver, and return the blob.
    """
    ret = self.generic(
        branch='',
        issue=12853011,
        patchset=1,
        project='chrome',
        repository='',
        requester='commit-bot@chromium.org',
        revision='HEAD',
        rietveld='https://codereview.chromium.org',
        patch_project='chromium',
    )
    ret.properties.update(kwargs)
    return ret
